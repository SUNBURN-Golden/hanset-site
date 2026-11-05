"""Conservative, auditable queue repair without requests or row deletion."""
from collections import Counter
from datetime import datetime
from urllib.parse import urlsplit
from .db import deterministic_id
from .util.followups import allows_followup, portal_domains, FOCUSED_STRATEGIES
from .util.timeutil import kst_now
from .util.urls import registrable_domain

def prune_offsite_followups(db, clock=None, apply=False):
    vias=tuple('reverse:'+s for s in FOCUSED_STRATEGIES); portals=portal_domains()
    venues={r['venue_id']:dict(r) for r in db.execute('SELECT venue_id,domains,website FROM venues')}
    rows=[dict(r) for r in db.execute("SELECT * FROM tasks WHERE state='pending' AND discovered_via IN (?,?,?,?)",vias)
        if not allows_followup(venues.get(r['venue_id']),r['url'],r['discovered_via'].split(':',1)[1],portals)]
    if apply:
        now=kst_now(clock).isoformat()
        with db:
            for row in rows:
                db.execute("UPDATE tasks SET state='dead',lease_until=NULL,updated_at=? WHERE task_id=?",(now,row['task_id']))
                db.execute("INSERT INTO failures(failure_id,task_id,url,domain,reason,state,created_at) VALUES (?,?,?,?,'pruned_offsite_followup','dead',?) ON CONFLICT(task_id,url,reason) DO UPDATE SET state='dead'",
                    (deterministic_id(row['task_id'],'pruned_offsite_followup'),row['task_id'],row['url'],row['domain'],now))
    return {'total':len(rows),'by_domain':dict(Counter(r['domain'] for r in rows)),
        'by_discovered_via':dict(Counter(r['discovered_via'] for r in rows))}

def requeue_robots_trials(db, clock=None):
    """Reopen expired transient failures, plus one HTTP-to-HTTPS upgrade trial."""
    now=kst_now(clock); latest={}; reopened=[]
    seeds={r[0] for r in db.execute('SELECT DISTINCT domain FROM seed_domains')}
    for row in db.execute('SELECT * FROM robots_cache'):
        domain=registrable_domain(row['origin'])
        if domain in seeds and (domain not in latest or datetime.fromisoformat(row['fetched_at'])>datetime.fromisoformat(latest[domain]['fetched_at'])):
            latest[domain]=row
    with db:
        for domain,row in latest.items():
            status=row['status']
            # Cached HTTP unavailability can be reused by the HTTPS fallback;
            # this one-time migration need not wait for expiry or retry budget.
            upgrade_retry=(urlsplit(row['origin']).scheme=='http' and (status is None or 500<=status<600)
                and not db.execute("SELECT 1 FROM failures WHERE domain=? AND reason='https_upgrade_retry'",(domain,)).fetchone())
            if not upgrade_retry and (datetime.fromisoformat(row['expires_at'])>=now or not (status is None or status==429 or 500<=status<600 or status in (301,302,303,307,308))): continue
            source=db.execute("SELECT * FROM sources WHERE domain=? AND state='inaccessible' AND reason='robots_disallowed'",(domain,)).fetchone()
            if not source: continue
            if db.execute("SELECT 1 FROM tasks WHERE domain=? AND kind='trial' AND state IN ('pending','running')",(domain,)).fetchone(): continue
            task=db.execute("SELECT t.* FROM tasks t WHERE domain=? AND kind='trial' AND state='dead' AND EXISTS (SELECT 1 FROM failures f WHERE f.task_id=t.task_id AND f.reason='robots_disallowed' AND f.state='dead') ORDER BY created_at,task_id LIMIT 1",(domain,)).fetchone()
            if not task: continue
            attempts=db.execute("SELECT max(max(attempts,repeat_failures)) FROM tasks WHERE domain=? AND kind='trial'",(domain,)).fetchone()[0] or 0
            retries=db.execute("SELECT coalesce(max(attempts),0) FROM failures WHERE domain=? AND reason='robots_unavailable_retry'",(domain,)).fetchone()[0]
            retries=max(retries,attempts-1)
            if retries>=3 and not upgrade_retry: continue
            reason='https_upgrade_retry' if upgrade_retry else 'robots_unavailable_retry'
            stamp=now.isoformat()
            # Preserve the initial failure plus this retry even if the run
            # stops before execution. Audit counts also cover other trial URLs.
            db.execute("UPDATE tasks SET state='pending',next_at=?,lease_until=NULL,updated_at=?,repeat_failures=? WHERE task_id=?",(stamp,stamp,retries+2,task['task_id']))
            # Candidate state forces a fresh relevance trial after robots succeeds.
            db.execute("UPDATE sources SET state='candidate',reason=?,next_at=? WHERE domain=?",(reason,stamp,domain))
            db.execute("INSERT INTO failures(failure_id,task_id,url,domain,reason,state,attempts,next_at,created_at) VALUES (?,?,?,?,?,'pending',?,?,?) ON CONFLICT(task_id,url,reason) DO UPDATE SET attempts=excluded.attempts,state='pending',next_at=excluded.next_at",
                (deterministic_id(task['task_id'],reason),task['task_id'],task['url'],domain,reason,1 if upgrade_retry else retries+1,stamp,stamp))
            reopened.append(domain)
    return reopened

def scrub_portal_venue_domains(db, clock=None):
    """Drop portal registrable domains from venues.domains/website; audited via review_queue."""
    import json
    from .venues import review
    portals=portal_domains(); changed=[]
    with db:
        for row in db.execute('SELECT venue_id,website,domains FROM venues').fetchall():
            try: domains=json.loads(row['domains'] or '[]')
            except ValueError: continue
            keep=[d for d in domains if registrable_domain(d if '://' in d else 'https://'+d) not in portals]
            website='' if row['website'] and registrable_domain(row['website']) in portals else row['website']
            if keep==domains and website==row['website']: continue
            db.execute('UPDATE venues SET domains=?,website=? WHERE venue_id=?',(json.dumps(keep),website,row['venue_id']))
            review(db,'portal_domain_scrubbed',row['venue_id'],{'removed_domains':sorted(set(domains)-set(keep)),'old_website':row['website']},clock)
            changed.append(row['venue_id'])
    return changed
