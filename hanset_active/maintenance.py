"""Conservative, auditable queue repair without requests or row deletion."""
from collections import Counter
from datetime import datetime
import json
from urllib.parse import urlsplit
from .db import deterministic_id
from .util.followups import allows_followup, portal_domains, FOCUSED_STRATEGIES
from .util.timeutil import kst_now
from .util.urls import canonicalize, is_denied, is_excluded, registrable_domain

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

def _accept_domain(domain, portals):
    if not domain or domain in portals: return ''
    probe='https://'+domain
    if is_denied(probe) or is_excluded(domain, probe): return ''
    return domain

def _add_domain(found, value, portals):
    if not isinstance(value, str): return
    text=value.strip()
    if not text: return
    try:
        domain=registrable_domain(text if '://' in text else 'https://'+text)
    except (ValueError, UnicodeError):
        return
    domain=_accept_domain(domain, portals)
    if domain: found.add(domain)

def _tracked_domains(db):
    """Seed domains plus venue websites, without portals or denied hosts.

    Directory and other source rows are not venue sites. visitseoul.net,
    wikipedia, openstreetmap, and similar origins stay out unless a tracked
    venue actually lists that registrable domain.
    """
    portals=portal_domains()
    found=set()
    for (domain,) in db.execute('SELECT DISTINCT domain FROM seed_domains'):
        _add_domain(found, domain, portals)
    for row in db.execute('SELECT website,domains FROM venues'):
        _add_domain(found, row['website'], portals)
        try:
            listed=json.loads(row['domains'] or '[]')
        except ValueError:
            continue
        if isinstance(listed, list):
            for item in listed: _add_domain(found, item, portals)
    return found

def _latest_seed_robots(db):
    latest={}
    tracked=_tracked_domains(db)
    for row in db.execute('SELECT * FROM robots_cache'):
        domain=registrable_domain(row['origin'])
        if domain in tracked and (domain not in latest or datetime.fromisoformat(row['fetched_at'])>datetime.fromisoformat(latest[domain]['fetched_at'])):
            latest[domain]=row
    return latest

def _https_url(url):
    parts=urlsplit(url)
    if parts.scheme!='http': return url
    return canonicalize(parts._replace(scheme='https').geturl())

def _robots_permits(row, url):
    """Cached origin was fetchable and its robots.txt allows this URL."""
    if not row['allowed']: return False
    from urllib.robotparser import RobotFileParser
    from .fetcher import USER_AGENT
    parser=RobotFileParser()
    parser.parse((row['body'] or '').splitlines())
    return bool(parser.can_fetch(USER_AGENT, url))

def requeue_robots_trials(db, clock=None):
    """Reopen expired transient failures, plus one HTTP-to-HTTPS upgrade trial.

    Seeds and tracked venue sites are eligible. Directory sources are not.
    The one-shot HTTPS upgrade rewrites an http trial URL to that same host
    and path on https.
    """
    now=kst_now(clock); reopened=[]
    latest=_latest_seed_robots(db)
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
            # An HTTP trial reopened for the one-shot upgrade must itself request
            # HTTPS, so the next fetch reads that origin's robots.txt and still
            # fails closed when the HTTPS robots file is unavailable.
            url=_https_url(task['url']) if upgrade_retry else task['url']
            # Preserve the initial failure plus this retry even if the run
            # stops before execution. Audit counts also cover other trial URLs.
            db.execute("UPDATE tasks SET state='pending',url=?,next_at=?,lease_until=NULL,updated_at=?,repeat_failures=? WHERE task_id=?",(url,stamp,stamp,retries+2,task['task_id']))
            # Candidate state forces a fresh relevance trial after robots succeeds.
            db.execute("UPDATE sources SET state='candidate',reason=?,next_at=? WHERE domain=?",(reason,stamp,domain))
            db.execute("INSERT INTO failures(failure_id,task_id,url,domain,reason,state,attempts,next_at,created_at) VALUES (?,?,?,?,?,'pending',?,?,?) ON CONFLICT(task_id,url,reason) DO UPDATE SET attempts=excluded.attempts,state='pending',next_at=excluded.next_at",
                (deterministic_id(task['task_id'],reason),task['task_id'],url,domain,reason,1 if upgrade_retry else retries+1,stamp,stamp))
            reopened.append(domain)
    return reopened

# One domain-level reopen. The task then uses the normal non-terminal attempt cap.
SPA_SHELL_RETRY_LIMIT=1
BROWSER_AUX_SOFTFAIL_RETRY='browser_aux_softfail_retry'

def _render_failure(reason):
    """A browser render died. Aux-poisoned renders were stored as ``error``."""
    text=(reason or '').strip().casefold()
    return text=='error' or ('render' in text and 'fail' in text)

def requeue_spa_shell_trials(db, clock=None):
    """Reopen one dead empty_body trial when the latest robots cache allows it.

    The domain is a seed or a tracked venue site. Recorded as spa_shell_retry
    and not repeated for that domain. Attempts reset so the reopen gets the
    normal retry cap. Inaccessible sources and disallowed robots are left alone.
    """
    now=kst_now(clock); reopened=[]
    latest=_latest_seed_robots(db)
    with db:
        for domain,row in latest.items():
            if db.execute("SELECT 1 FROM tasks WHERE domain=? AND kind='trial' AND state IN ('pending','running')",(domain,)).fetchone(): continue
            task=db.execute("SELECT * FROM tasks WHERE domain=? AND kind='trial' ORDER BY updated_at DESC,created_at DESC,task_id LIMIT 1",(domain,)).fetchone()
            if not task or task['state']!='dead' or not _robots_permits(row, task['url']): continue
            death=db.execute("SELECT reason FROM failures WHERE task_id=? AND state='dead' ORDER BY created_at DESC,failure_id DESC LIMIT 1",(task['task_id'],)).fetchone()
            if not death or death['reason']!='empty_body': continue
            retries=db.execute("SELECT coalesce(max(attempts),0) FROM failures WHERE domain=? AND reason='spa_shell_retry'",(domain,)).fetchone()[0]
            if retries>=SPA_SHELL_RETRY_LIMIT: continue
            source=db.execute('SELECT state FROM sources WHERE domain=?',(domain,)).fetchone()
            if source and source['state']=='inaccessible': continue
            reason='spa_shell_retry'
            stamp=now.isoformat()
            # Previous attempts never rendered. Start one normal retry cycle.
            db.execute("UPDATE tasks SET state='pending',attempts=0,repeat_failures=0,next_at=?,lease_until=NULL,updated_at=? WHERE task_id=?",(stamp,stamp,task['task_id']))
            if source and source['state']=='candidate':
                db.execute("UPDATE sources SET reason=?,next_at=? WHERE domain=?",(reason,stamp,domain))
            db.execute("INSERT INTO failures(failure_id,task_id,url,domain,reason,state,attempts,next_at,created_at) VALUES (?,?,?,?,?,'pending',?,?,?) ON CONFLICT(task_id,url,reason) DO UPDATE SET attempts=excluded.attempts,state='pending',next_at=excluded.next_at",
                (deterministic_id(task['task_id'],reason),task['task_id'],task['url'],domain,reason,1,stamp,stamp))
            reopened.append(domain)
    return reopened

def requeue_browser_aux_softfail_trials(db, clock=None):
    """One more reopen when spa_shell_retry already died on a render error.

    The empty-body reopen was spent while a single aux asset failure aborted
    the browser render (outcome ``error``). ``browser_aux_softfail_retry`` is
    domain-wide and not repeated. Robots must allow the trial URL. Inaccessible
    sources stay dead. Attempts reset so the reopen gets a normal retry cap.
    """
    now=kst_now(clock); reopened=[]
    latest=_latest_seed_robots(db)
    with db:
        for domain,row in latest.items():
            if db.execute("SELECT 1 FROM tasks WHERE domain=? AND kind='trial' AND state IN ('pending','running')",(domain,)).fetchone(): continue
            task=db.execute("SELECT * FROM tasks WHERE domain=? AND kind='trial' ORDER BY updated_at DESC,created_at DESC,task_id LIMIT 1",(domain,)).fetchone()
            if not task or task['state']!='dead' or not _robots_permits(row, task['url']): continue
            death=db.execute("SELECT reason FROM failures WHERE task_id=? AND state='dead' ORDER BY created_at DESC,failure_id DESC LIMIT 1",(task['task_id'],)).fetchone()
            if not death or not _render_failure(death['reason']): continue
            if not db.execute("SELECT 1 FROM failures WHERE domain=? AND reason='spa_shell_retry'",(domain,)).fetchone(): continue
            if db.execute("SELECT 1 FROM failures WHERE domain=? AND reason=?",(domain,BROWSER_AUX_SOFTFAIL_RETRY)).fetchone(): continue
            source=db.execute('SELECT state FROM sources WHERE domain=?',(domain,)).fetchone()
            if source and source['state']=='inaccessible': continue
            reason=BROWSER_AUX_SOFTFAIL_RETRY
            stamp=now.isoformat()
            db.execute("UPDATE tasks SET state='pending',attempts=0,repeat_failures=0,next_at=?,lease_until=NULL,updated_at=? WHERE task_id=?",(stamp,stamp,task['task_id']))
            if source and source['state']=='candidate':
                db.execute("UPDATE sources SET reason=?,next_at=? WHERE domain=?",(reason,stamp,domain))
            db.execute("INSERT INTO failures(failure_id,task_id,url,domain,reason,state,attempts,next_at,created_at) VALUES (?,?,?,?,?,'pending',?,?,?) ON CONFLICT(task_id,url,reason) DO UPDATE SET attempts=excluded.attempts,state='pending',next_at=excluded.next_at",
                (deterministic_id(task['task_id'],reason),task['task_id'],task['url'],domain,reason,1,stamp,stamp))
            reopened.append(domain)
    return reopened

def scrub_portal_venue_domains(db, clock=None):
    """Drop portal registrable domains from venues.domains/website; audited via review_queue."""
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
