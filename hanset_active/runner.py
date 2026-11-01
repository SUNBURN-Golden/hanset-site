import os
"""Bounded, restartable single-process collection runs."""
import json
import math
import signal
import threading
import uuid
from urllib.parse import urlsplit
from datetime import datetime, timedelta
from .db import deterministic_id
from .executor import Executor, BudgetExhausted, TaskFailure
from .locks import RunLock, AlreadyRunning
from .scheduler import Scheduler, enqueue, TASK_CLASSES, refresh_days
from . import gaps
from .discover.search import queries, usable_providers
from .util.timeutil import kst_now
from .util.pii import strip_pii
from .maintenance import prune_offsite_followups, requeue_robots_trials, scrub_portal_venue_domains

BACKOFF_SECONDS = (600, 3600, 21600)

def recover(db, *, clock=None):
    """Recover expired task leases and due failed tasks, preserving attempt count."""
    now = kst_now(clock).isoformat()
    with db:
        db.execute("UPDATE tasks SET kind='trial' WHERE kind IS NULL AND url IS NOT NULL")
        db.execute("UPDATE tasks SET state='pending',lease_until=NULL WHERE state='running' AND (lease_until IS NULL OR lease_until<=?)",(now,))
        db.execute("UPDATE tasks SET state='pending' WHERE state='failed' AND next_at<=? AND attempts<3",(now,))
        db.execute("UPDATE failures SET state='pending' WHERE state='failed' AND next_at<=? AND attempts<3",(now,))


def replenish_search(fetcher):
    """Materialize at most 200 pending searches; leave blocked providers dormant."""
    db, clock = fetcher.db, fetcher.clock
    if not usable_providers(db,clock=clock): return
    count=db.execute("SELECT count(*) FROM tasks WHERE kind='search' AND state IN ('pending','running')").fetchone()[0]
    for query,lang in queries(fetcher.settings.profile):
        if count>=200: break
        before=db.total_changes
        enqueue(db,kind='search',payload={'query':query,'lang':lang},clock=clock,discovered_via='profile:search',novelty=1)
        count+=int(db.total_changes>before)


def bootstrap(fetcher):
    """Register discovery work; task dedupe makes this safe on each invocation."""
    db, clock, profile = fetcher.db, fetcher.clock, fetcher.settings.profile
    replenish_search(fetcher)
    for directory in profile.get('directories',[]):
        if directory.get('enabled',True):
            tid=enqueue(db,kind='directory',payload=directory,clock=clock,task_class='discover',discovered_via='profile:directory',novelty=1,source_quality=1)
            # A completed root is revisited on the next run. Candidate dedupe
            # lets bounded sitemaps continue past already registered entries.
            with db:
                db.execute("UPDATE tasks SET state='pending',next_at=NULL WHERE task_id=? AND state='done'",(tid,))
    for row in db.execute("SELECT s.* FROM seed_venues s JOIN run_checkpoints c ON c.checkpoint_id='seed_freeze' AND c.cursor=s.seed_id").fetchall():
        if row['website']: fetcher.register_candidate(row['website'])
    now = kst_now(clock)
    for gap in db.execute('SELECT * FROM gaps WHERE next_at<=?',(now.isoformat(),)).fetchall():
        gaps.reopen(db,gap['gap_id'],clock=clock)
    for raw in db.execute('SELECT * FROM raw_documents r WHERE fetched_at=(SELECT max(fetched_at) FROM raw_documents WHERE canonical_url=r.canonical_url)').fetchall():
        source = db.execute('SELECT state FROM sources WHERE source_id=?',(raw['source_id'],)).fetchone()
        if source and source[0]=='inaccessible': continue
        observed = [r[0] for r in db.execute('SELECT DISTINCT field FROM observations WHERE source_url=?',(raw['canonical_url'],))]
        days = min((refresh_days(f,deprioritized=bool(source and source[0]=='deprioritized')) for f in observed),default=30)
        if (now-datetime.fromisoformat(raw['fetched_at'])).total_seconds()>=days*86400:
            enqueue(db,kind='fetch',url=raw['canonical_url'],task_class='freshness',clock=clock,
                dedupe_key=deterministic_id('freshness',raw['canonical_url'],raw['fetched_at'],now.date().isoformat()),
                discovered_via='freshness:'+raw['canonical_url'],urgency=(now-datetime.fromisoformat(raw['fetched_at'])).total_seconds()/(days*86400))


def fail_task(db, task, error, *, clock=None):
    attempts = task['attempts'] + 1
    terminal = isinstance(error,TaskFailure) and error.terminal
    state = 'dead' if terminal or attempts >= 3 else 'failed'
    next_at = (kst_now(clock)+timedelta(seconds=BACKOFF_SECONDS[min(attempts-1,2)])).isoformat()
    if isinstance(error,TaskFailure) and error.retry_after:
        next_at = max(next_at,error.retry_after)
    reason = strip_pii(str(error) if isinstance(error,TaskFailure) else type(error).__name__)
    fid = deterministic_id(task['task_id'],task.get('url') or '',reason)
    now = kst_now(clock).isoformat()
    with db:
        db.execute('UPDATE tasks SET state=?,attempts=?,repeat_failures=?,next_at=?,lease_until=NULL,updated_at=? WHERE task_id=?',
            (state,attempts,attempts,next_at,now,task['task_id']))
        # Reuse a fetcher's failure row if it already recorded this outcome.
        existing = db.execute('SELECT failure_id FROM failures WHERE task_id=? AND reason=?',(task['task_id'],reason)).fetchone()
        if existing: fid = existing[0]
        db.execute('INSERT INTO failures(failure_id,task_id,url,domain,reason,state,attempts,next_at,created_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(failure_id) DO UPDATE SET state=excluded.state,attempts=excluded.attempts,next_at=excluded.next_at',
            (fid,task['task_id'],task.get('url'),task.get('domain'),reason,state,attempts,next_at,now))


def run(fetcher, *, max_minutes=None, max_requests=None, classes=None, dry_run=False, providers=None, initialize=True):
    profile = fetcher.settings.profile
    max_minutes = float(max_minutes if max_minutes is not None else profile['budgets']['run_minutes'])
    max_requests = max_requests if max_requests is not None else profile['budgets']['run_requests']
    if not math.isfinite(max_minutes) or max_minutes<0 or not isinstance(max_requests,int) or max_requests<0:
        raise ValueError('Run limits must be finite and nonnegative')
    selected = set(classes.split(',') if isinstance(classes,str) else classes or TASK_CLASSES)
    if not selected or not selected <= set(TASK_CLASSES): raise ValueError('Unknown task classes')
    db, clock = fetcher.db, fetcher.clock
    run_id = uuid.uuid4().hex
    deadline = clock.monotonic()+60*max_minutes
    try:
        with RunLock(db,fetcher.settings.data_dir,run_id,clock=clock) as lock:
            if dry_run:
                return {'state':'dry_run','tasks':[dict(r) for r in db.execute("SELECT * FROM tasks WHERE state='pending'") if r['task_class'] in selected], 'requests':0}
            scrub_portal_venue_domains(db,clock=clock)
            prune_offsite_followups(db,clock=clock,apply=True)
            for domain in requeue_robots_trials(db,clock=clock):
                fetcher._stopped.pop(domain,None)
            recover(db,clock=clock)
            # With exclusive flock, all remaining running tasks are orphaned:
            # do not wait for a killed process's future lease to expire.
            with db:
                db.execute("UPDATE tasks SET state='pending',lease_until=NULL WHERE state='running'")
                db.execute("UPDATE runs SET state='interrupted',finished_at=? WHERE state='running'",(kst_now(clock).isoformat(),))
                db.execute('INSERT INTO runs(run_id,started_at,state,max_requests,max_minutes) VALUES (?,?,?,?,?)',(run_id,kst_now(clock).isoformat(),'running',max_requests,max_minutes))
            if initialize: bootstrap(fetcher)
            executor = Executor(fetcher,providers=providers,max_requests=max_requests,deadline=deadline)
            executor.run_id = run_id
            executor.heartbeat = lock.heartbeat
            executor.install()
            scheduler = Scheduler(db,clock=clock,budget=max_requests,shares=profile['budgets'].get('class_shares'),discover_floor=profile['budgets'].get('discover_floor',.25),trial_limits={urlsplit(d['url']).hostname:min(5,d.get('max_candidates',5)) for d in profile.get('directories',[]) if d.get('low_prior')})
            stopped = threading.Event()
            old_handler = None
            if threading.current_thread() is threading.main_thread():
                old_handler = signal.signal(signal.SIGTERM,lambda *_: stopped.set())
            completed = 0
            state = 'done'
            try:
                while not stopped.is_set() and clock.monotonic()<deadline and executor.requests<max_requests:
                    recover(db,clock=clock)
                    if initialize: replenish_search(fetcher)
                    # Filter before the scheduler so excluded classes consume no share.
                    pending = [dict(r) for r in db.execute("SELECT * FROM tasks WHERE state='pending'") if r['task_class'] in selected]
                    if not pending:
                        failed = [r for r in db.execute("SELECT * FROM tasks WHERE state='failed' AND attempts<3") if r['task_class'] in selected and r['next_at']]
                        if not failed: break
                        delay = min((datetime.fromisoformat(r['next_at'])-kst_now(clock)).total_seconds() for r in failed)
                        if clock.monotonic()+max(0,delay)>=deadline: break
                        clock.sleep(min(60,max(0,delay)))
                        lock.heartbeat()
                        continue
                    task = _pick(scheduler,selected,deadline,{domain:last+max(fetcher.settings.min_interval,fetcher._delay.get(domain,0)) for domain,last in fetcher._last.items() if domain not in fetcher._stopped})
                    if task is None: break
                    until = lock.heartbeat()
                    with db:
                        db.execute("UPDATE tasks SET state='running',lease_until=?,updated_at=? WHERE task_id=?",(until,kst_now(clock).isoformat(),task['task_id']))
                    try:
                        executor.execute(task)
                    except BudgetExhausted:
                        if os.environ.get('HANSET_ACTIVE_DEBUG'): print('BUDGET', task['kind'], task['url'], executor.requests, max_requests, clock.monotonic()-deadline)
                        with db: db.execute("UPDATE tasks SET state='pending',lease_until=NULL WHERE task_id=?",(task['task_id'],))
                        state = 'budget_reached'
                        break
                    except Exception as error:
                        if os.environ.get('HANSET_ACTIVE_DEBUG'):
                            import traceback; traceback.print_exc()
                        fail_task(db,task,error,clock=clock)
                    else:
                        with db:
                            db.execute("UPDATE tasks SET state='done',lease_until=NULL,updated_at=? WHERE task_id=?",(kst_now(clock).isoformat(),task['task_id']))
                            db.execute("UPDATE failures SET state='resolved' WHERE task_id=? AND state!='dead'",(task['task_id'],))
                            db.execute('INSERT OR REPLACE INTO run_checkpoints VALUES (?,?,?,?,?,?)',(deterministic_id(run_id,task['task_id'],'done'),run_id,task['task_id'],'done',json.dumps({'url':task['url']}),kst_now(clock).isoformat()))
                        completed += 1
                if stopped.is_set(): state = 'stopped'
                elif executor.requests>=max_requests or clock.monotonic()>=deadline: state = 'budget_reached'
            finally:
                executor.close()
                if old_handler is not None: signal.signal(signal.SIGTERM,old_handler)
                with db:
                    db.execute('UPDATE runs SET finished_at=?,state=?,requests=?,stats=? WHERE run_id=?',(kst_now(clock).isoformat(),state,executor.requests,json.dumps({'completed':completed}),run_id))
            return {'state':state,'run_id':run_id,'requests':executor.requests,'completed':completed}
    except AlreadyRunning:
        return {'state':'already_running','requests':0}


def _pick(scheduler, selected, deadline, cooldowns=None):
    """Preserve scheduler fairness without allowing unselected task classes."""
    # The Stage B picker has no class predicate. Its queue can be filtered with
    # a read-only connection facade; no queue states are changed for filtering.
    original = scheduler.db
    class Filtered:
        def execute(self, sql, params=()):
            if "WHERE state='pending'" in sql:
                sql = sql.replace("WHERE state='pending'", "WHERE state='pending' AND task_class IN ("+','.join('?' for _ in selected)+')')
                params = tuple(sorted(selected))+tuple(params)
            return original.execute(sql,params)
    scheduler.db = Filtered()
    try: return scheduler.pick(deadline=deadline,cooldowns=cooldowns)
    finally: scheduler.db = original
