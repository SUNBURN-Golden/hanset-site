from datetime import timedelta
import json
import pytest
from hanset_active.cli import main
from hanset_active.maintenance import prune_offsite_followups, requeue_robots_trials
from hanset_active.runner import run, fail_task
from hanset_active.executor import TaskFailure
from hanset_active.scheduler import enqueue
from hanset_active.util.timeutil import kst_now
from hanset_active.venues import upsert_venue

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

def dead_trial(bot,status=None, *, expired=True,attempts=1,domain='example.com'):
    now=kst_now(bot.clock)
    with bot.db:
        bot.db.execute('INSERT INTO seed_domains VALUES (?,?,?,?)',(domain,'fixture',domain,now.isoformat()))
        bot.db.execute("INSERT INTO sources(source_id,domain,state,reason) VALUES (?,?,'inaccessible','robots_disallowed')",(domain,domain))
        bot.db.execute('INSERT INTO robots_cache VALUES (?,?,?,?,?,?,?)',('https://'+domain,'User-agent: *\nDisallow: /',status,(now-timedelta(days=2)).isoformat(),(now+timedelta(days=-1 if expired else 1)).isoformat(),int(status==200),'robots unavailable'))
    tid=enqueue(bot.db,kind='trial',url='https://'+domain,clock=bot.clock)
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
