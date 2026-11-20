from datetime import timedelta
import json
import pytest
from hanset_active.cli import main
from hanset_active.db import deterministic_id
from hanset_active.maintenance import (close_article_title_venues, prune_offsite_followups,
                                       requeue_browser_aux_softfail_trials, requeue_robots_trials,
                                       requeue_spa_shell_trials)
from hanset_active.runner import run, fail_task
from hanset_active.executor import TaskFailure
from hanset_active.scheduler import enqueue
from hanset_active.util.timeutil import kst_now
from hanset_active.venues import upsert_venue


def test_article_title_maintenance_runs_once_and_preserves_evidence(bot):
    db = bot.db
    with db:
        db.executemany('INSERT INTO venues(venue_id,venue_key,name,state) VALUES (?,?,?,?)', [
            ('legacy', 'legacy', 'Seed Spa A 추천!', 'active'),
            ('already-closed', 'already-closed', 'Box B TOP 5', 'closed'),
            ('keep', 'keep', 'Seed Spa A', 'unknown'),
        ])
        db.execute('INSERT INTO observations(obs_id,venue_key,field,value_hash,label_status) VALUES (?,?,?,?,?)',
                   ('legacy-obs', 'legacy', 'hours', 'fixture', 'source_claim'))
    before = list(db.iterdump())
    assert run(bot, dry_run=True, initialize=False)['state'] == 'dry_run'
    assert list(db.iterdump()) == before
    assert run(bot, max_requests=0, initialize=False)['state'] == 'budget_reached'
    assert dict(db.execute('SELECT venue_id,state FROM venues').fetchall()) == {
        'legacy': 'closed', 'already-closed': 'closed', 'keep': 'unknown',
    }
    assert [tuple(row) for row in db.execute('SELECT kind,venue_id,details FROM review_queue')] == [
        ('article_title', 'legacy', '{"reason": "article_title"}'),
    ]
    assert db.execute('SELECT label_status FROM observations').fetchone()[0] == 'source_claim'
    changes = db.total_changes
    assert close_article_title_venues(db, clock=bot.clock) == 0
    assert db.total_changes == changes

def test_prune_followups(bot):
    vid=upsert_venue(bot.db,'Example Sauna',website='https://example.com')
    rejected=[]
    for strategy in ('site_links','lang_counterpart','attachments','images'):
        rejected.append(enqueue(bot.db,kind='fetch',url='https://portal.example/'+strategy,venue_id=vid,discovered_via='reverse:'+strategy))
    rejected.append(enqueue(bot.db,kind='fetch',url='https://example.com/en/home',venue_id=vid,discovered_via='reverse:lang_counterpart'))
    rejected.append(enqueue(bot.db,kind='fetch',url='https://example.com/price',discovered_via='reverse:site_links'))
    kept=enqueue(bot.db,kind='fetch',url='https://example.com/price',venue_id=vid,discovered_via='reverse:site_links')
    unrelated=enqueue(bot.db,kind='fetch',url='https://portal.example/other',venue_id=vid,discovered_via='links')
    done=enqueue(bot.db,kind='fetch',url='https://portal.example/done',venue_id=vid,discovered_via='reverse:images')
    bot.db.execute("UPDATE tasks SET state='done' WHERE task_id=?",(done,)); bot.db.commit()
    changes=bot.db.total_changes
    result=prune_offsite_followups(bot.db,clock=bot.clock)
    assert result=={'total':6,'by_domain':{'portal.example':4,'example.com':2},
        'by_discovered_via':{'reverse:site_links':2,'reverse:lang_counterpart':2,'reverse:attachments':1,'reverse:images':1}}
    assert bot.db.total_changes==changes
    assert prune_offsite_followups(bot.db,clock=bot.clock,apply=True)==result
    states={r['task_id']:r['state'] for r in bot.db.execute('SELECT * FROM tasks')}
    assert all(states[t]=='dead' for t in rejected)
    assert states[kept]==states[unrelated]=='pending' and states[done]=='done'
    failures=list(bot.db.execute('SELECT * FROM failures'))
    assert len(failures)==6
    assert all(r['reason']=='pruned_offsite_followup' and r['state']=='dead' and r['url'] and r['domain'] for r in failures)
    changes=bot.db.total_changes
    assert prune_offsite_followups(bot.db,apply=True)['total']==0
    assert bot.db.total_changes==changes

def test_prune_cli(bot,capsys):
    tid=enqueue(bot.db,kind='fetch',url='https://portal.example/price',discovered_via='reverse:site_links')
    args=['--data-dir',str(bot.settings.data_dir)]
    # The CLI opens its normal database filename outside the checkout.
    from hanset_active.db import connect
    db=connect(bot.settings.data_dir/'active.sqlite3')
    bot.db.backup(db); db.close()
    assert main(args+['prune-followups'])==0
    assert json.loads(capsys.readouterr().out)['total']==1
    db=connect(bot.settings.data_dir/'active.sqlite3')
    assert db.execute('SELECT state FROM tasks WHERE task_id=?',(tid,)).fetchone()[0]=='pending'
    db.close()
    assert main(args+['prune-followups','--apply'])==0
    assert json.loads(capsys.readouterr().out)['total']==1
    assert main(args+['prune-followups','--apply'])==0
    assert json.loads(capsys.readouterr().out)['total']==0

def dead_trial(bot,status=None, *, expired=True,attempts=1,domain='example.com',scheme='https'):
    now=kst_now(bot.clock)
    with bot.db:
        bot.db.execute('INSERT INTO seed_domains VALUES (?,?,?,?)',(domain,'fixture',domain,now.isoformat()))
        bot.db.execute("INSERT INTO sources(source_id,domain,state,reason) VALUES (?,?,'inaccessible','robots_disallowed')",(deterministic_id(domain),domain))
        bot.db.execute('INSERT INTO robots_cache VALUES (?,?,?,?,?,?,?)',(scheme+'://'+domain,'User-agent: *\nDisallow: /',status,(now-timedelta(days=2)).isoformat(),(now+timedelta(days=-1 if expired else 1)).isoformat(),int(status==200),'robots unavailable'))
    tid=enqueue(bot.db,kind='trial',url=scheme+'://'+domain,clock=bot.clock)
    task=dict(bot.db.execute('SELECT * FROM tasks WHERE task_id=?',(tid,)).fetchone())
    task['attempts']=attempts-1
    fail_task(bot.db,task,TaskFailure('robots_disallowed',terminal=True),clock=bot.clock)
    return tid

@pytest.mark.parametrize('status',[None,500,503,429,301,302,303,307,308])
def test_requeue_transient_robots(bot,status):
    tid=dead_trial(bot,status)
    # Multiple eligible trials on one domain must not all reopen.
    other=enqueue(bot.db,kind='trial',url='https://example.com/other',clock=bot.clock)
    fail_task(bot.db,dict(bot.db.execute('SELECT * FROM tasks WHERE task_id=?',(other,)).fetchone()),TaskFailure('robots_disallowed',terminal=True),clock=bot.clock)
    assert requeue_robots_trials(bot.db,clock=bot.clock)==['example.com']
    assert bot.db.execute("SELECT count(*) FROM tasks WHERE state='pending'").fetchone()[0]==1
    task=bot.db.execute("SELECT * FROM tasks WHERE state='pending'").fetchone()
    assert task['next_at']==kst_now(bot.clock).isoformat()
    assert bot.db.execute('SELECT state FROM sources').fetchone()[0]=='candidate'
    assert bot.db.execute("SELECT attempts FROM failures WHERE reason='robots_unavailable_retry'").fetchone()[0]==1
    assert requeue_robots_trials(bot.db,clock=bot.clock)==[]
    assert bot.db.execute('SELECT status FROM robots_cache').fetchone()[0]==status

@pytest.mark.parametrize('status,expired,attempts',[(200,True,1),(401,True,1),(403,True,1),(500,False,1),(301,True,4)])
def test_requeue_keeps_ineligible_dead(bot,status,expired,attempts):
    tid=dead_trial(bot,status,expired=expired,attempts=attempts)
    assert requeue_robots_trials(bot.db,clock=bot.clock)==[]
    assert bot.db.execute('SELECT state FROM tasks WHERE task_id=?',(tid,)).fetchone()[0]=='dead'
    assert bot.db.execute('SELECT state FROM sources').fetchone()[0]=='inaccessible'

def test_requeue_three_retry_domain_limit(bot):
    tid=dead_trial(bot,500)
    for retry in range(3):
        assert requeue_robots_trials(bot.db,clock=bot.clock)==['example.com']
        task=dict(bot.db.execute('SELECT * FROM tasks WHERE task_id=?',(tid,)).fetchone())
        fail_task(bot.db,task,TaskFailure('robots_disallowed',terminal=True),clock=bot.clock)
        bot.db.execute("UPDATE sources SET state='inaccessible',reason='robots_disallowed'"); bot.db.commit()
    assert requeue_robots_trials(bot.db,clock=bot.clock)==[]
    assert bot.db.execute("SELECT attempts FROM failures WHERE reason='robots_unavailable_retry'").fetchone()[0]==3

@pytest.mark.parametrize('status',[None,500,503])
@pytest.mark.parametrize('expired',[False,True])
def test_requeue_https_upgrade_once_after_retry_cap(bot,status,expired):
    domain='example-sauna.test'
    tid=dead_trial(bot,status,expired=expired,attempts=4,domain=domain,scheme='http')
    now=kst_now(bot.clock).isoformat()
    with bot.db:
        bot.db.execute("INSERT INTO failures(failure_id,task_id,url,domain,reason,state,attempts,created_at) VALUES (?,?,?,?,'robots_unavailable_retry','resolved',3,?)",
            ('retry-audit',tid,'http://'+domain,domain,now))
    # Multiple dead trial URLs must share one upgrade allowance.
    other=enqueue(bot.db,kind='trial',url='http://'+domain+'/other',clock=bot.clock)
    fail_task(bot.db,dict(bot.db.execute('SELECT * FROM tasks WHERE task_id=?',(other,)).fetchone()),TaskFailure('robots_disallowed',terminal=True),clock=bot.clock)
    assert requeue_robots_trials(bot.db,clock=bot.clock)==[domain]
    pending=bot.db.execute("SELECT * FROM tasks WHERE state='pending'").fetchall()
    assert len(pending)==1 and pending[0]['next_at']==now
    assert pending[0]['url'] in ('https://'+domain,'https://'+domain+'/other')
    dead=bot.db.execute("SELECT task_id,url FROM tasks WHERE state='dead'").fetchone()
    assert {pending[0]['task_id'],dead['task_id']}=={tid,other} and dead['url'].startswith('http://'+domain)
    assert bot.db.execute('SELECT state,reason FROM sources').fetchone()[:]==('candidate','https_upgrade_retry')
    audit=bot.db.execute("SELECT * FROM failures WHERE reason='https_upgrade_retry'").fetchone()
    assert audit['domain']==domain and audit['attempts']==1 and audit['url']==pending[0]['url']
    assert bot.db.execute("SELECT attempts FROM failures WHERE reason='robots_unavailable_retry'").fetchone()[0]==3
    assert requeue_robots_trials(bot.db,clock=bot.clock)==[]
    fail_task(bot.db,dict(pending[0]),TaskFailure('robots_disallowed',terminal=True),clock=bot.clock)
    with bot.db:
        bot.db.execute("UPDATE sources SET state='inaccessible',reason='robots_disallowed'")
        # Resolution and robots expiry must not reset the domain-wide marker.
        bot.db.execute("UPDATE failures SET state='resolved' WHERE reason='https_upgrade_retry'")
    bot.clock.sleep(3*86400)
    assert requeue_robots_trials(bot.db,clock=bot.clock)==[]
    assert bot.db.execute("SELECT count(*) FROM failures WHERE reason='https_upgrade_retry'").fetchone()[0]==1

@pytest.mark.parametrize('status',[200,401,403,429,301])
def test_https_upgrade_requeue_only_for_unavailable_http(bot,status):
    tid=dead_trial(bot,status,attempts=4,domain='example-sauna.test',scheme='http')
    assert requeue_robots_trials(bot.db,clock=bot.clock)==[]
    assert bot.db.execute('SELECT state FROM tasks WHERE task_id=?',(tid,)).fetchone()[0]=='dead'
    assert not bot.db.execute("SELECT 1 FROM failures WHERE reason='https_upgrade_retry'").fetchone()

def test_run_reopens_http_trial_and_fetches_https(bot,monkeypatch):
    from types import SimpleNamespace
    domain='example-sauna.test'
    tid=dead_trial(bot,500,expired=False,attempts=4,domain=domain,scheme='http')
    bot._stopped[domain]='robots_disallowed'
    routes={
        'https://'+domain+'/robots.txt':b'User-agent: *\nAllow: /',
        'https://'+domain:('<title>Example Sauna</title><main>'+('Sauna spa venue open daily in Seoul. '*8)+'</main>').encode(),
    }
    calls=[]
    def get(url,**kwargs):
        calls.append(url)
        body=routes[url]
        return SimpleNamespace(status_code=200,headers={'Content-Type':'text/plain' if url.endswith('robots.txt') else 'text/html'},
            iter_content=lambda _:iter([body]),close=lambda:None)
    monkeypatch.setattr(bot.session,'get',get)
    assert run(bot,max_requests=2,initialize=False,providers=[])['requests']==2
    assert calls==list(routes)
    assert bot.db.execute('SELECT url,state FROM tasks WHERE task_id=?',(tid,)).fetchone()[:]==('https://'+domain,'done')
    assert bot.db.execute('SELECT state FROM sources').fetchone()[0]!='inaccessible'
    assert domain not in bot._stopped
    assert bot.db.execute("SELECT state FROM failures WHERE reason='https_upgrade_retry'").fetchone()[0]=='resolved'

@pytest.mark.parametrize('seed',[False,True])
def test_https_upgrade_requeue_requires_seed_and_latest_http(bot,seed):
    domain='example-sauna.test'
    dead_trial(bot,500,attempts=4,domain=domain,scheme='http')
    now=kst_now(bot.clock)
    with bot.db:
        if not seed:
            bot.db.execute('DELETE FROM seed_domains')
        else:
            bot.db.execute('INSERT INTO robots_cache VALUES (?,?,?,?,?,?,?)',('https://'+domain,'',403,now.isoformat(),(now-timedelta(seconds=1)).isoformat(),0,'robots unavailable'))
    assert requeue_robots_trials(bot.db,clock=bot.clock)==[]

def test_run_maintains_queue_before_requests(bot):
    tid=dead_trial(bot,301)
    offsite=enqueue(bot.db,kind='fetch',url='https://portal.example/price',discovered_via='reverse:site_links')
    bot._stopped['example.com']='robots_disallowed'
    assert run(bot,max_requests=0,initialize=False)['requests']==0
    states={r['task_id']:r['state'] for r in bot.db.execute('SELECT * FROM tasks')}
    assert states[tid]=='pending' and states[offsite]=='dead'
    assert 'example.com' not in bot._stopped

def test_run_dry_run_does_not_maintain_queue(bot):
    tid=dead_trial(bot,500)
    offsite=enqueue(bot.db,kind='fetch',url='https://portal.example/price',discovered_via='reverse:site_links')
    assert run(bot,max_requests=0,initialize=False,dry_run=True)['state']=='dry_run'
    states={r['task_id']:r['state'] for r in bot.db.execute('SELECT * FROM tasks')}
    assert states[tid]=='dead' and states[offsite]=='pending'

def test_requeue_latest_robots_and_seed_only(bot):
    tid=dead_trial(bot,500)
    now=kst_now(bot.clock)
    with bot.db:
        bot.db.execute('INSERT INTO robots_cache VALUES (?,?,?,?,?,?,?)',('https://www.example.com','',403,now.isoformat(),(now-timedelta(seconds=1)).isoformat(),0,'robots unavailable'))
    assert requeue_robots_trials(bot.db,clock=bot.clock)==[]
    with bot.db:
        bot.db.execute('DELETE FROM robots_cache WHERE origin=?',('https://www.example.com',))
        bot.db.execute('DELETE FROM seed_domains')
    assert requeue_robots_trials(bot.db,clock=bot.clock)==[]

def test_requeue_audit_limit_survives_another_trial(bot):
    tid=dead_trial(bot,500)
    for _ in range(3):
        assert requeue_robots_trials(bot.db,clock=bot.clock)==['example.com']
        task=dict(bot.db.execute('SELECT * FROM tasks WHERE task_id=?',(tid,)).fetchone())
        fail_task(bot.db,task,TaskFailure('robots_disallowed',terminal=True),clock=bot.clock)
        with bot.db: bot.db.execute("UPDATE sources SET state='inaccessible',reason='robots_disallowed'")
    other=enqueue(bot.db,kind='trial',url='https://example.com/other',clock=bot.clock)
    fail_task(bot.db,dict(bot.db.execute('SELECT * FROM tasks WHERE task_id=?',(other,)).fetchone()),TaskFailure('robots_disallowed',terminal=True),clock=bot.clock)
    assert requeue_robots_trials(bot.db,clock=bot.clock)==[]

def fail_times(bot, tid, reason, times=1, *, terminal=False):
    for _ in range(times):
        task=dict(bot.db.execute('SELECT * FROM tasks WHERE task_id=?',(tid,)).fetchone())
        fail_task(bot.db,task,TaskFailure(reason,terminal=terminal),clock=bot.clock)
    return dict(bot.db.execute('SELECT * FROM tasks WHERE task_id=?',(tid,)).fetchone())

def stuck_shell(bot, *, domain='example-sauna.test', seed=True, source_state='candidate', source_reason='https_upgrade_retry', death='empty_body', https=True, https_allowed=1, https_body='User-agent: *\nAllow: /\n', https_status=200, expired=False, spent=False):
    """HTTP seed trial that died empty_body; optional newer HTTPS robots cache."""
    now=kst_now(bot.clock)
    url='http://'+domain
    with bot.db:
        if seed:
            bot.db.execute('INSERT INTO seed_domains VALUES (?,?,?,?)',(domain,'fixture',domain,now.isoformat()))
        if source_state:
            bot.db.execute('INSERT INTO sources(source_id,domain,state,reason) VALUES (?,?,?,?)',(deterministic_id(domain),domain,source_state,source_reason))
        bot.db.execute('INSERT INTO robots_cache VALUES (?,?,?,?,?,?,?)',(url,'',500,(now-timedelta(days=2)).isoformat(),(now-timedelta(days=1)).isoformat(),0,'robots unavailable'))
    tid=enqueue(bot.db,kind='trial',url=url,clock=bot.clock)
    terminal=death in ('robots_disallowed','blocked','login_wall')
    fail_times(bot,tid,death,1 if terminal else 3,terminal=terminal)
    if death=='empty_body':
        past=(kst_now(bot.clock)-timedelta(days=3)).isoformat()
        with bot.db:
            bot.db.execute("INSERT INTO failures(failure_id,task_id,url,domain,reason,state,attempts,created_at) VALUES (?,?,?,?,'robots_disallowed','dead',1,?)",
                (deterministic_id(tid,'old-robots'),tid,url,domain,past))
    if https:
        bot.clock.sleep(2)
        stamp=kst_now(bot.clock)
        with bot.db:
            bot.db.execute('INSERT INTO robots_cache VALUES (?,?,?,?,?,?,?)',(
                'https://'+domain,https_body,https_status,stamp.isoformat(),(stamp+timedelta(days=-1 if expired else 1)).isoformat(),int(https_allowed),'' if https_allowed else 'robots unavailable'))
    if spent:
        stamp=kst_now(bot.clock).isoformat()
        with bot.db:
            bot.db.execute("INSERT INTO failures(failure_id,task_id,url,domain,reason,state,attempts,created_at) VALUES (?,?,?,?,'spa_shell_retry','pending',1,?)",
                (deterministic_id(tid,'spa_shell_retry'),tid,url,domain,stamp))
    return tid

@pytest.mark.parametrize('expired',[False,True])
def test_requeue_spa_shell_reopens_latest_empty_seed_once(bot,expired):
    domain='example-sauna.test'
    home=stuck_shell(bot,domain=domain,expired=expired)
    bot.clock.sleep(1)
    latest=enqueue(bot.db,kind='trial',url='http://'+domain+'/visit',clock=bot.clock)
    assert fail_times(bot,latest,'empty_body',3)['state']=='dead'
    assert bot.db.execute('SELECT attempts,state FROM tasks WHERE task_id=?',(latest,)).fetchone()[:]==(3,'dead')
    assert requeue_spa_shell_trials(bot.db,clock=bot.clock)==[domain]
    opened=bot.db.execute('SELECT * FROM tasks WHERE task_id=?',(latest,)).fetchone()
    assert opened['state']=='pending' and opened['attempts']==0 and opened['repeat_failures']==0
    assert opened['url']=='http://'+domain+'/visit' and opened['next_at']==kst_now(bot.clock).isoformat()
    assert bot.db.execute('SELECT state FROM tasks WHERE task_id=?',(home,)).fetchone()[0]=='dead'
    assert bot.db.execute('SELECT state,reason FROM sources').fetchone()[:]==('candidate','spa_shell_retry')
    assert bot.db.execute('SELECT status,allowed FROM robots_cache WHERE origin=?',('http://'+domain,)).fetchone()[:]==(500,0)
    audit=bot.db.execute("SELECT * FROM failures WHERE reason='spa_shell_retry'").fetchone()
    assert audit['attempts']==1 and audit['state']=='pending' and audit['domain']==domain and audit['task_id']==latest
    assert requeue_spa_shell_trials(bot.db,clock=bot.clock)==[]
    dead=fail_times(bot,latest,'empty_body',3)
    assert dead['state']=='dead' and dead['attempts']==3
    assert requeue_spa_shell_trials(bot.db,clock=bot.clock)==[]
    assert bot.db.execute('SELECT state FROM tasks WHERE task_id=?',(home,)).fetchone()[0]=='dead'
    assert bot.db.execute("SELECT count(*),max(attempts) FROM failures WHERE reason='spa_shell_retry'").fetchone()[:]==(1,1)

def test_requeue_spa_shell_without_source_row(bot):
    tid=stuck_shell(bot,source_state=None)
    assert requeue_spa_shell_trials(bot.db,clock=bot.clock)==['example-sauna.test']
    assert bot.db.execute('SELECT count(*) FROM sources').fetchone()[0]==0
    assert bot.db.execute('SELECT state,attempts FROM tasks WHERE task_id=?',(tid,)).fetchone()[:]==('pending',0)

@pytest.mark.parametrize('kwargs', [
    pytest.param(dict(https=False), id='latest-robots-unavailable'),
    pytest.param(dict(https_allowed=0, https_status=403, https_body='User-agent: *\nDisallow: /\n'), id='latest-robots-disallowed'),
    pytest.param(dict(https_body='User-agent: *\nDisallow: /\n'), id='path-disallowed'),
    pytest.param(dict(source_state='inaccessible', source_reason='blocked'), id='blocked'),
    pytest.param(dict(source_state='inaccessible', source_reason='login_wall'), id='login-wall'),
    pytest.param(dict(source_state='inaccessible', source_reason='robots_disallowed'), id='robots-stop'),
    pytest.param(dict(spent=True), id='retry-budget-spent'),
    pytest.param(dict(seed=False), id='not-a-seed'),
    pytest.param(dict(death='robots_disallowed'), id='died-robots'),
    pytest.param(dict(death='blocked', source_state='inaccessible', source_reason='blocked'), id='died-blocked'),
])
def test_requeue_spa_shell_skips_unsafe_or_spent(bot,kwargs):
    tid=stuck_shell(bot,**kwargs)
    before_source=tuple(bot.db.execute('SELECT state,reason FROM sources').fetchone())
    before=bot.db.execute("SELECT count(*) FROM failures WHERE reason='spa_shell_retry'").fetchone()[0]
    assert requeue_spa_shell_trials(bot.db,clock=bot.clock)==[]
    assert bot.db.execute('SELECT state FROM tasks WHERE task_id=?',(tid,)).fetchone()[0]=='dead'
    assert bot.db.execute("SELECT count(*) FROM failures WHERE reason='spa_shell_retry'").fetchone()[0]==before
    assert tuple(bot.db.execute('SELECT state,reason FROM sources').fetchone())==before_source

def test_requeue_spa_shell_leaves_pending_trial(bot):
    home=stuck_shell(bot)
    other=enqueue(bot.db,kind='trial',url='https://example-sauna.test/other',clock=bot.clock)
    assert requeue_spa_shell_trials(bot.db,clock=bot.clock)==[]
    assert bot.db.execute('SELECT state FROM tasks WHERE task_id=?',(home,)).fetchone()[0]=='dead'
    assert bot.db.execute('SELECT state FROM tasks WHERE task_id=?',(other,)).fetchone()[0]=='pending'
    assert not bot.db.execute("SELECT 1 FROM failures WHERE reason='spa_shell_retry'").fetchone()

def test_run_reopens_spa_shell_before_requests(bot):
    tid=stuck_shell(bot)
    assert run(bot,max_requests=0,initialize=False)['requests']==0
    assert bot.db.execute('SELECT state,attempts FROM tasks WHERE task_id=?',(tid,)).fetchone()[:]==('pending',0)
    assert bot.db.execute("SELECT reason,attempts FROM failures WHERE reason='spa_shell_retry'").fetchone()[:]==('spa_shell_retry',1)

def test_dry_run_does_not_reopen_spa_shell(bot):
    tid=stuck_shell(bot)
    assert run(bot,max_requests=0,initialize=False,dry_run=True)['state']=='dry_run'
    assert bot.db.execute('SELECT state FROM tasks WHERE task_id=?',(tid,)).fetchone()[0]=='dead'
    assert not bot.db.execute("SELECT 1 FROM failures WHERE reason='spa_shell_retry'").fetchone()

def test_run_renders_reopened_spa_trial(bot,monkeypatch):
    from types import SimpleNamespace
    from hanset_active.extract import browser
    from hanset_active.fetcher import FetchResult
    domain='example-sauna.test'
    tid=stuck_shell(bot,domain=domain)
    shell=b'<html><div id="app"></div><script src="/app.js"></script></html>'
    rendered=('<html><title>Example Sauna</title><main><h1>Example Sauna</h1><p>'
        +'Seoul sauna spa hotel day pass 25000 KRW. Opening hours 09:00 ~ 22:00. '*3
        +'</p></main></html>').encode()
    routes={
        'http://'+domain+'/robots.txt':(500,'text/plain',b''),
        'https://'+domain+'/robots.txt':(200,'text/plain',b'User-agent: *\nAllow: /\n'),
        'https://'+domain:(200,'text/html',shell),
    }
    calls=[]
    def get(url,**kwargs):
        calls.append(url)
        status,ctype,body=routes[url]
        return SimpleNamespace(status_code=status,headers={'Content-Type':ctype},iter_content=lambda _:iter([body]),close=lambda:None)
    seen=[]
    def render(fetcher,url):
        seen.append(url)
        return FetchResult(url,'ok',status=200,content=rendered,content_type='text/html',fetch_method='browser')
    monkeypatch.setattr(bot.session,'get',get)
    monkeypatch.setattr(browser,'fetch',render)
    assert run(bot,max_requests=6,initialize=False,providers=[])['completed']>=1
    assert calls[0]=='http://'+domain+'/robots.txt' and 'https://'+domain in calls
    assert seen==['https://'+domain]
    assert bot.db.execute('SELECT state FROM tasks WHERE task_id=?',(tid,)).fetchone()[0]=='done'
    assert bot.db.execute('SELECT fetch_method FROM raw_documents WHERE url=?',('https://'+domain,)).fetchone()[0]=='browser'
    assert bot.db.execute("SELECT state FROM failures WHERE reason='spa_shell_retry'").fetchone()[0]=='resolved'

def raw_dead_http(bot, domain, *, status=None, path=''):
    """Dead http robots trial inserted directly, including hosts enqueue would refuse."""
    now=kst_now(bot.clock)
    url='http://'+domain+path
    tid=deterministic_id('raw-trial',domain)
    with bot.db:
        bot.db.execute("INSERT INTO sources(source_id,domain,state,reason) VALUES (?,?,'inaccessible','robots_disallowed')",(deterministic_id(domain),domain))
        bot.db.execute('INSERT INTO robots_cache VALUES (?,?,?,?,?,?,?)',('http://'+domain,'',status,(now-timedelta(days=2)).isoformat(),(now-timedelta(days=1)).isoformat(),0,'robots unavailable'))
        bot.db.execute("INSERT INTO tasks(task_id,dedupe_key,url,domain,kind,task_class,state,attempts,created_at,updated_at) VALUES (?,?,?,?,'trial','discover','dead',4,?,?)",(tid,tid,url,domain,now.isoformat(),now.isoformat()))
        bot.db.execute("INSERT INTO failures(failure_id,task_id,url,domain,reason,state,attempts,created_at) VALUES (?,?,?,?,'robots_disallowed','dead',1,?)",(deterministic_id(tid,'robots'),tid,url,domain,now.isoformat()))
    return tid

def test_transient_robots_retry_keeps_http_url(bot):
    domain='example-sauna.test'
    tid=dead_trial(bot,301,domain=domain,scheme='http')
    assert requeue_robots_trials(bot.db,clock=bot.clock)==[domain]
    assert bot.db.execute('SELECT url FROM tasks WHERE task_id=?',(tid,)).fetchone()[0]=='http://'+domain
    assert bot.db.execute('SELECT reason FROM sources').fetchone()[0]=='robots_unavailable_retry'

def test_https_upgrade_reopens_venue_domain_not_portals_or_sources(bot):
    from hanset_active.util.followups import portal_domains
    wanted='spa-venue.co.kr'
    portals=portal_domains()
    assert {'visitseoul.net','openstreetmap.org','tistory.com'} <= portals
    assert 'wikipedia.org' not in portals and 'accuweather.com' not in portals
    blocked={
        'visitseoul.net':'https://korean.visitseoul.net/spa',
        'tistory.com':'https://venue.tistory.com/1',
        'openstreetmap.org':'https://nominatim.openstreetmap.org/search',
        'instagram.com':'https://instagram.com/venue',
        'runport.example':'https://runport.example',
    }
    loose=('wikipedia.org','accuweather.com')
    ids={domain:raw_dead_http(bot,domain) for domain in (wanted,*blocked,*loose)}
    with bot.db:
        bot.db.execute('UPDATE tasks SET url=? WHERE task_id=?',('http://'+wanted+'/visit',ids[wanted]))
    assert requeue_robots_trials(bot.db,clock=bot.clock)==[]
    vid=upsert_venue(bot.db,'Example Spa',website='https://placeholder.example')
    listed=[wanted,'www.'+wanted,*blocked.values()]
    with bot.db:
        bot.db.execute('UPDATE venues SET website=?,domains=? WHERE venue_id=?',('',json.dumps(listed),vid))
    assert requeue_robots_trials(bot.db,clock=bot.clock)==[wanted]
    pending=bot.db.execute('SELECT url,state FROM tasks WHERE task_id=?',(ids[wanted],)).fetchone()
    assert pending['state']=='pending' and pending['url']=='https://'+wanted+'/visit'
    audit=bot.db.execute("SELECT url,attempts,state FROM failures WHERE reason='https_upgrade_retry'").fetchone()
    assert audit['url']=='https://'+wanted+'/visit' and audit['attempts']==1 and audit['state']=='pending'
    for domain in (*blocked,*loose):
        assert bot.db.execute('SELECT state FROM tasks WHERE task_id=?',(ids[domain],)).fetchone()[0]=='dead'
    assert requeue_robots_trials(bot.db,clock=bot.clock)==[]

def test_https_upgrade_uses_venue_website_when_domains_empty(bot):
    domain='ispaland.test'
    tid=raw_dead_http(bot,domain)
    vid=upsert_venue(bot.db,'Example Spa',website='http://'+domain)
    with bot.db:
        bot.db.execute("UPDATE venues SET domains='[]' WHERE venue_id=?",(vid,))
    assert requeue_robots_trials(bot.db,clock=bot.clock)==[domain]
    assert bot.db.execute('SELECT url FROM tasks WHERE task_id=?',(tid,)).fetchone()[0]=='https://'+domain

def test_https_upgrade_keeps_website_when_domains_json_is_invalid(bot):
    domain='bongil.test'
    tid=raw_dead_http(bot,domain)
    vid=upsert_venue(bot.db,'Example Spa',website='http://'+domain)
    with bot.db:
        bot.db.execute('UPDATE venues SET domains=? WHERE venue_id=?',('{',vid))
    assert requeue_robots_trials(bot.db,clock=bot.clock)==[domain]
    assert bot.db.execute('SELECT url FROM tasks WHERE task_id=?',(tid,)).fetchone()[0]=='https://'+domain

def test_requeue_spa_shell_includes_unseeded_venue(bot):
    tid=stuck_shell(bot,seed=False)
    assert requeue_spa_shell_trials(bot.db,clock=bot.clock)==[]
    upsert_venue(bot.db,'Example Sauna',website='http://example-sauna.test')
    assert requeue_spa_shell_trials(bot.db,clock=bot.clock)==['example-sauna.test']
    assert bot.db.execute('SELECT state,attempts FROM tasks WHERE task_id=?',(tid,)).fetchone()[:]==('pending',0)

@pytest.mark.parametrize('expired',[False,True])
@pytest.mark.parametrize('reason',['error','render failed'])
def test_requeue_browser_aux_softfail_once(bot,expired,reason):
    domain='example-sauna.test'
    tid=stuck_shell(bot,domain=domain,death=reason,spent=True,expired=expired)
    assert bot.db.execute('SELECT attempts,state FROM tasks WHERE task_id=?',(tid,)).fetchone()[:]==(3,'dead')
    assert requeue_browser_aux_softfail_trials(bot.db,clock=bot.clock)==[domain]
    opened=bot.db.execute('SELECT * FROM tasks WHERE task_id=?',(tid,)).fetchone()
    assert opened['state']=='pending' and opened['attempts']==0 and opened['repeat_failures']==0
    assert opened['next_at']==kst_now(bot.clock).isoformat()
    assert bot.db.execute('SELECT state,reason FROM sources').fetchone()[:]==('candidate','browser_aux_softfail_retry')
    audit=bot.db.execute("SELECT * FROM failures WHERE reason='browser_aux_softfail_retry'").fetchone()
    assert audit['attempts']==1 and audit['state']=='pending' and audit['domain']==domain and audit['task_id']==tid
    assert requeue_browser_aux_softfail_trials(bot.db,clock=bot.clock)==[]
    dead=fail_times(bot,tid,reason,3)
    assert dead['state']=='dead' and dead['attempts']==3
    with bot.db:
        bot.db.execute("UPDATE failures SET state='resolved' WHERE reason='browser_aux_softfail_retry'")
        bot.db.execute("UPDATE sources SET state='candidate',reason='spa_shell_retry'")
    assert requeue_browser_aux_softfail_trials(bot.db,clock=bot.clock)==[]
    assert bot.db.execute("SELECT count(*),max(attempts) FROM failures WHERE reason='browser_aux_softfail_retry'").fetchone()[:]==(1,1)

def test_requeue_browser_aux_reopens_latest_trial_only(bot):
    domain='example-sauna.test'
    home=stuck_shell(bot,domain=domain,death='error',spent=True)
    bot.clock.sleep(1)
    latest=enqueue(bot.db,kind='trial',url='http://'+domain+'/visit',clock=bot.clock)
    assert fail_times(bot,latest,'error',3)['state']=='dead'
    assert requeue_browser_aux_softfail_trials(bot.db,clock=bot.clock)==[domain]
    assert bot.db.execute('SELECT state,attempts,url FROM tasks WHERE task_id=?',(latest,)).fetchone()[:]==('pending',0,'http://'+domain+'/visit')
    assert bot.db.execute('SELECT state FROM tasks WHERE task_id=?',(home,)).fetchone()[0]=='dead'
    assert bot.db.execute("SELECT task_id FROM failures WHERE reason='browser_aux_softfail_retry'").fetchone()[0]==latest

def test_requeue_browser_aux_without_source_row(bot):
    tid=stuck_shell(bot,source_state=None,death='error',spent=True)
    assert requeue_browser_aux_softfail_trials(bot.db,clock=bot.clock)==['example-sauna.test']
    assert bot.db.execute('SELECT count(*) FROM sources').fetchone()[0]==0
    assert bot.db.execute('SELECT state,attempts FROM tasks WHERE task_id=?',(tid,)).fetchone()[:]==('pending',0)

def test_requeue_browser_aux_venue_without_seed(bot):
    tid=stuck_shell(bot,seed=False,death='error',spent=True)
    assert requeue_browser_aux_softfail_trials(bot.db,clock=bot.clock)==[]
    upsert_venue(bot.db,'Example Sauna',website='http://example-sauna.test')
    assert requeue_browser_aux_softfail_trials(bot.db,clock=bot.clock)==['example-sauna.test']
    assert bot.db.execute('SELECT state,attempts FROM tasks WHERE task_id=?',(tid,)).fetchone()[:]==('pending',0)

@pytest.mark.parametrize('kwargs',[
    pytest.param(dict(spent=False),id='no-prior-spa-retry'),
    pytest.param(dict(death='empty_body',spent=True),id='latest-death-still-empty'),
    pytest.param(dict(death='blocked',source_state='inaccessible',source_reason='blocked',spent=True),id='blocked'),
    pytest.param(dict(https_allowed=0,https_status=403,https_body='User-agent: *\nDisallow: /\n',spent=True),id='robots-disallow'),
    pytest.param(dict(seed=False,spent=True),id='not-seed-or-venue'),
    pytest.param(dict(source_state='inaccessible',source_reason='robots_disallowed',spent=True),id='inaccessible'),
    pytest.param(dict(death='robots_disallowed',source_state='inaccessible',source_reason='robots_disallowed',spent=True),id='died-robots'),
])
def test_requeue_browser_aux_skips_ineligible(bot,kwargs):
    kwargs.setdefault('death','error')
    tid=stuck_shell(bot,**kwargs)
    before=bot.db.execute("SELECT count(*) FROM failures WHERE reason='browser_aux_softfail_retry'").fetchone()[0]
    assert requeue_browser_aux_softfail_trials(bot.db,clock=bot.clock)==[]
    assert bot.db.execute('SELECT state FROM tasks WHERE task_id=?',(tid,)).fetchone()[0]=='dead'
    assert bot.db.execute("SELECT count(*) FROM failures WHERE reason='browser_aux_softfail_retry'").fetchone()[0]==before

def test_requeue_browser_aux_leaves_pending_trial(bot):
    home=stuck_shell(bot,death='error',spent=True)
    other=enqueue(bot.db,kind='trial',url='https://example-sauna.test/other',clock=bot.clock)
    assert requeue_browser_aux_softfail_trials(bot.db,clock=bot.clock)==[]
    assert bot.db.execute('SELECT state FROM tasks WHERE task_id=?',(home,)).fetchone()[0]=='dead'
    assert bot.db.execute('SELECT state FROM tasks WHERE task_id=?',(other,)).fetchone()[0]=='pending'
    assert not bot.db.execute("SELECT 1 FROM failures WHERE reason='browser_aux_softfail_retry'").fetchone()

def test_run_reopens_browser_aux_before_requests(bot):
    tid=stuck_shell(bot,death='error',spent=True)
    assert run(bot,max_requests=0,initialize=False)['requests']==0
    assert bot.db.execute('SELECT state,attempts FROM tasks WHERE task_id=?',(tid,)).fetchone()[:]==('pending',0)
    assert bot.db.execute("SELECT reason,attempts FROM failures WHERE reason='browser_aux_softfail_retry'").fetchone()[:]==('browser_aux_softfail_retry',1)

def test_dry_run_does_not_reopen_browser_aux(bot):
    tid=stuck_shell(bot,death='error',spent=True)
    assert run(bot,max_requests=0,initialize=False,dry_run=True)['state']=='dry_run'
    assert bot.db.execute('SELECT state FROM tasks WHERE task_id=?',(tid,)).fetchone()[0]=='dead'
    assert not bot.db.execute("SELECT 1 FROM failures WHERE reason='browser_aux_softfail_retry'").fetchone()
