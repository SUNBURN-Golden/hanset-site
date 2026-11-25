"""Conservative, auditable queue repair without requests or row deletion."""
from collections import Counter
from datetime import datetime
import json
from urllib.parse import urlsplit
from .crawl_policy import KoreanNewsRobots, is_korean_news
from .db import deterministic_id
from .fetcher import USER_AGENT
from .host_policy import origin
from .locks import hostname
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
    portals=portal_domains() - {'naver.com', 'daum.net'}
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

def _robots_class(url):
    """Only an effective HTTP URL with a usable hostname proves a policy class."""
    try:
        canonicalize(url, preserve_path=True)
        parts = urlsplit(url)
        if hostname(url) not in ('naver.com', 'daum.net') or parts.path not in ('', '/'):
            return 'news' if is_korean_news(url) else 'non_news'
    except (ValueError, TypeError, UnicodeError):
        pass
    return 'unresolved'


def reopen_non_news_robots(db, clock=None):
    """Reopen legacy work only when its stored effective denial URL proves eligibility."""
    stamp = kst_now(clock).isoformat()
    counts = dict(reopened_tasks=0, reopened_sources=0, kept_news=0, unresolved=0)
    reason = 'robots_policy_reopened'
    with db:
        failures = db.execute("""
            SELECT f.* FROM failures f JOIN tasks t ON f.task_id=t.task_id
            WHERE f.reason='robots_disallowed' AND f.state='dead' AND t.state='dead'
              AND NOT EXISTS (SELECT 1 FROM failures audit
                              WHERE audit.task_id=t.task_id AND audit.reason=?)
              AND NOT EXISTS (SELECT 1 FROM failures last WHERE last.task_id=t.task_id
                              AND last.state='dead' AND last.reason!='robots_disallowed'
                              AND last.created_at>=f.created_at)
        """, (reason,)).fetchall()
        # Tied historical rows can include different redirect destinations.
        by_task = {}
        for failure in failures:
            by_task.setdefault(failure['task_id'], []).append(failure)
        for task_id, rows in by_task.items():
            classes = {_robots_class(row['url']) for row in rows}
            if 'news' in classes:
                counts['kept_news'] += 1
            elif classes != {'non_news'}:
                counts['unresolved'] += 1
            elif db.execute("""UPDATE tasks SET state='pending', attempts=0, repeat_failures=0,
                              next_at=NULL, lease_until=NULL, updated_at=?
                              WHERE task_id=? AND state='dead'""", (stamp, task_id)).rowcount:
                failure = rows[0]
                db.execute("""INSERT INTO failures(failure_id,task_id,url,domain,reason,state,created_at)
                              VALUES (?,?,?,?,?,'pending',?)""",
                           (deterministic_id(task_id, reason), task_id, failure['url'], failure['domain'], reason, stamp))
                counts['reopened_tasks'] += 1

        tracked = _tracked_domains(db)
        for (domain,) in db.execute("""SELECT domain FROM sources
                    WHERE state='inaccessible' AND reason='robots_disallowed'""").fetchall():
            if domain not in tracked:
                continue
            classes = {_robots_class(row['url']) for row in db.execute(
                "SELECT url FROM failures WHERE domain=? AND reason='robots_disallowed'", (domain,))}
            if is_korean_news('https://' + domain) or 'news' in classes:
                counts['kept_news'] += 1
            elif classes != {'non_news'}:
                counts['unresolved'] += 1
            else:
                db.execute("""UPDATE sources SET state='candidate', reason=?, next_at=NULL
                              WHERE domain=?""", (reason, domain))
                counts['reopened_sources'] += 1
    return counts


def _robots_permits(url, news_cache, now):
    if not is_korean_news(url):
        return True
    if news_cache is None:
        return False
    row = news_cache.execute('SELECT body,expires_at FROM news_robots WHERE origin=?', (origin(url),)).fetchone()
    return bool(row and datetime.fromisoformat(row[1]) > now
                and KoreanNewsRobots(row[0], user_agent=USER_AGENT).can_fetch(url))


def _dead_trials(db):
    tracked = _tracked_domains(db)
    for domain in tracked:
        if db.execute("SELECT 1 FROM tasks WHERE domain=? AND kind='trial' AND state IN ('pending','running')",
                      (domain,)).fetchone():
            continue
        task = db.execute("""SELECT * FROM tasks WHERE domain=? AND kind='trial'
                            ORDER BY updated_at DESC,created_at DESC,task_id LIMIT 1""", (domain,)).fetchone()
        if task and task['state'] == 'dead':
            yield domain, task


# One domain-level reopen. The task then uses the normal non-terminal attempt cap.
SPA_SHELL_RETRY_LIMIT=1
BROWSER_AUX_SOFTFAIL_RETRY='browser_aux_softfail_retry'

def _render_failure(reason):
    """A browser render died. Aux-poisoned renders were stored as ``error``."""
    text=(reason or '').strip().casefold()
    return text=='error' or ('render' in text and 'fail' in text)

def requeue_spa_shell_trials(db, clock=None, *, news_cache=None):
    """Reopen one dead empty_body trial when the current policy permits it.

    The domain is a seed or a tracked venue site. Recorded as spa_shell_retry
    and not repeated for that domain. Attempts reset so the reopen gets the
    normal retry cap. Inaccessible sources and disallowed robots are left alone.
    """
    now=kst_now(clock); reopened=[]
    with db:
        for domain,task in _dead_trials(db):
            if not _robots_permits(task['url'], news_cache, now): continue
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

def requeue_browser_aux_softfail_trials(db, clock=None, *, news_cache=None):
    """One more reopen when spa_shell_retry already died on a render error.

    The empty-body reopen was spent while a single aux asset failure aborted
    the browser render (outcome ``error``). ``browser_aux_softfail_retry`` is
    domain-wide and not repeated. Robots must allow the trial URL. Inaccessible
    sources stay dead. Attempts reset so the reopen gets a normal retry cap.
    """
    now=kst_now(clock); reopened=[]
    with db:
        for domain,task in _dead_trials(db):
            if not _robots_permits(task['url'], news_cache, now): continue
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

def close_article_title_venues(db, clock=None):
    """Close invalid identities and record the correction without touching evidence."""
    from .venues import is_article_title, review

    changed = 0
    with db:
        for row in db.execute("SELECT venue_id, name FROM venues WHERE state IS NOT 'closed'").fetchall():
            if is_article_title(row['name']):
                db.execute("UPDATE venues SET state='closed' WHERE venue_id=?", (row['venue_id'],))
                review(db, 'article_title', row['venue_id'], {'reason': 'article_title'}, clock)
                changed += 1
    return changed


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
