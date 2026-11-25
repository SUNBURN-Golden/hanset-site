from datetime import timedelta
import json
import pytest
from hanset_active.cli import main
from hanset_active.db import deterministic_id
from hanset_active.maintenance import (close_article_title_venues, prune_offsite_followups,
                                       reopen_non_news_robots, requeue_browser_aux_softfail_trials,
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

def dead_trial(bot, requested, effective, *, source=True):
    tid = enqueue(bot.db, kind='trial', url=requested, clock=bot.clock)
    task = dict(bot.db.execute('SELECT * FROM tasks WHERE task_id=?', (tid,)).fetchone())
    fail_task(bot.db, task, TaskFailure('robots_disallowed', terminal=True), clock=bot.clock)
    with bot.db:
        bot.db.execute('UPDATE failures SET url=? WHERE task_id=?', (effective, tid))
        if source:
            domain = task['domain']
            bot.db.execute('INSERT OR IGNORE INTO seed_domains VALUES (?,?,?,?)',
                           (domain, 'fixture', domain, kst_now(bot.clock).isoformat()))
            bot.db.execute("""INSERT OR IGNORE INTO sources(source_id,domain,state,reason)
                              VALUES (?,?,'inaccessible','robots_disallowed')""", (deterministic_id(domain), domain))
    return tid


def test_reopen_requires_effective_non_news_url(bot):
    old = dead_trial(bot, 'https://spa.test/hours', 'https://spa.test/hours')
    naver = dead_trial(bot, 'https://naver.com/visit', 'https://blog.naver.com/visit')
    ambiguous = dead_trial(bot, 'https://daum.net/visit', 'https://daum.net')
    redirected = dead_trial(bot, 'https://portal.test/visit', 'https://n.news.naver.com/visit')
    news = dead_trial(bot, 'https://news.daum.net/visit', 'https://news.daum.net/visit')
    missing = dead_trial(bot, 'https://other.test/visit', None)
    unrelated = dead_trial(bot, 'https://another.test/visit', 'https://another.test/visit', source=False)
    bot.clock.sleep(1)
    fail_times(bot, unrelated, 'login_wall', terminal=True)
    assert reopen_non_news_robots(bot.db, bot.clock) == {
        'reopened_tasks': 2, 'reopened_sources': 2, 'kept_news': 4, 'unresolved': 3,
    }
    assert dict(bot.db.execute('SELECT task_id,state FROM tasks')) == {
        old: 'pending', naver: 'pending', redirected: 'dead', news: 'dead', missing: 'dead',
        ambiguous: 'dead', unrelated: 'dead',
    }
    assert bot.db.execute("SELECT count(*) FROM failures WHERE reason='robots_disallowed'").fetchone()[0] == 7
    assert bot.db.execute("SELECT count(*) FROM failures WHERE reason='robots_policy_reopened'").fetchone()[0] == 2
    again = reopen_non_news_robots(bot.db, bot.clock)
    assert again['reopened_tasks'] == again['reopened_sources'] == 0


def test_run_reopens_old_denial_before_requests(bot, monkeypatch):
    from types import SimpleNamespace
    url = 'https://spa.test/hours'
    tid = dead_trial(bot, url, url)
    offsite = enqueue(bot.db, kind='fetch', url='https://portal.test/price', discovered_via='reverse:site_links')
    body = ('<main>' + 'Seed Spa A spa venue open daily in Seoul. ' * 8 + '</main>').encode()
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=200, headers={'Content-Type': 'text/html'},
                               iter_content=lambda _:iter([body]), close=lambda:None)

    monkeypatch.setattr(bot.session, 'get', get)
    assert run(bot, dry_run=True, initialize=False)['requests'] == 0
    assert bot.db.execute('SELECT state FROM tasks WHERE task_id=?', (tid,)).fetchone()[0] == 'dead'
    result = run(bot, max_requests=1, initialize=False, providers=[])
    assert result['robots_reopen']['reopened_tasks'] == 1
    assert calls == [url]
    assert dict(bot.db.execute('SELECT task_id,state FROM tasks')) == {tid: 'done', offsite: 'dead'}


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
        'http://'+domain:(200,'text/html',shell),
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
    assert calls == ['http://'+domain]
    assert seen==['http://'+domain]
    assert bot.db.execute('SELECT state FROM tasks WHERE task_id=?',(tid,)).fetchone()[0]=='done'
    assert bot.db.execute('SELECT fetch_method FROM raw_documents WHERE url=?',('http://'+domain,)).fetchone()[0]=='browser'
    assert bot.db.execute("SELECT state FROM failures WHERE reason='spa_shell_retry'").fetchone()[0]=='resolved'

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


@pytest.mark.parametrize('resume,death,spent', [
    (requeue_spa_shell_trials, 'empty_body', False),
    (requeue_browser_aux_softfail_trials, 'error', True),
])
def test_news_render_reopen_needs_current_origin_policy(bot, monkeypatch, resume, death, spent):
    domain = 'news-fixture.test'
    monkeypatch.setattr('hanset_active.crawl_policy._NEWS_DOMAINS', {domain})
    stuck_shell(bot, domain=domain, death=death, spent=spent)
    now, news_cache = kst_now(bot.clock), bot.host_policy.db
    origin = 'http://' + domain
    expiry = (now + timedelta(hours=1)).isoformat()

    assert resume(bot.db, bot.clock, news_cache=news_cache) == []
    with news_cache:
        news_cache.execute('INSERT INTO news_robots VALUES (?,?,?)',
                           (origin, 'User-agent: *\nDisallow: /', expiry))
    assert resume(bot.db, bot.clock, news_cache=news_cache) == []
    with news_cache:
        news_cache.execute("UPDATE news_robots SET body=?,expires_at=? WHERE origin=?",
                           ('User-agent: *\nAllow: /', now.isoformat(), origin))
    assert resume(bot.db, bot.clock, news_cache=news_cache) == []
    with news_cache:
        news_cache.execute("UPDATE news_robots SET expires_at=? WHERE origin=?", (expiry, origin))
    assert resume(bot.db, bot.clock, news_cache=news_cache) == [domain]
    assert resume(bot.db, bot.clock, news_cache=news_cache) == []
