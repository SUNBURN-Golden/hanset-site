"""Idempotent task queue, class budgets and cooldown-aware task selection."""
import json
import math
from urllib.parse import urlsplit
from datetime import datetime, timedelta
from .config import load_profile
from .db import deterministic_id
from .util.timeutil import Clock, kst_now
from .util.pii import strip_pii
from .util.urls import canonicalize, registrable_domain, is_denied, is_excluded
TASK_CLASSES=('backfill','discover','freshness')

def refresh_days(field, *, deprioritized=False):
    if deprioritized or field in ('identity','links'): return 30
    return 3 if field in ('price_day_pass','hours','closures') else 14

def urgency(staleness_days, field, **kwargs):
    return max(0,staleness_days)/refresh_days(field,**kwargs)

def score(task, weights=None):
    weights=weights or load_profile()['scheduler_weights']
    return sum(weights[k]*float(task.get(k,0) or 0)*(-1 if k in ('cost','duplication','repeat_failures') else 1) for k in weights)

def split_budget(total, shares=None, discover_floor=.25):
    if total<0: raise ValueError('Negative budget')
    shares=shares or load_profile()['budgets']['class_shares']
    shares={k:max(0,float(shares.get(k,0))) for k in TASK_CLASSES}
    discover=max(.25,discover_floor,shares['discover']/sum(shares.values()) if sum(shares.values()) else .25)
    discover=min(1,discover)
    other=shares['backfill']+shares['freshness']
    fractions={'discover':discover,'backfill':(1-discover)*(shares['backfill']/other if other else .5)}
    fractions['freshness']=1-fractions['discover']-fractions['backfill']
    result={k:int(total*v) for k,v in fractions.items()}
    for k in sorted(fractions,key=lambda k:total*fractions[k]-result[k],reverse=True)[:total-sum(result.values())]: result[k]+=1
    floor=min(total,math.ceil(total*max(.25,discover_floor)))
    while result['discover']<floor:
        donor=max(('backfill','freshness'),key=result.get)
        result[donor]-=1;result['discover']+=1
    return result

def enqueue(db, *, kind, url='', task_class='discover', depth=0, payload=None, parent_url=None, parent_task_id=None, discovered_via='', venue_id=None, source_id=None, dedupe_key=None, clock=None, **metrics):
    if task_class not in TASK_CLASSES: raise ValueError('Unknown task class')
    if depth<0 or depth>2: return None
    if url:
        try: url=canonicalize(url)
        except ValueError: return None
        if is_denied(url) or is_excluded(url): return None
    safe_payload=strip_pii(json.dumps(payload or {},ensure_ascii=False,sort_keys=True))
    if is_excluded(safe_payload,discovered_via): return None
    key=dedupe_key or deterministic_id(kind,url,safe_payload,venue_id)
    task_id=deterministic_id(key); now=kst_now(clock).isoformat()
    if kind=='search' and not db.execute('SELECT 1 FROM tasks WHERE dedupe_key=?',(key,)).fetchone():
        if db.execute("SELECT count(*) FROM tasks WHERE kind='search' AND state IN ('pending','running')").fetchone()[0]>=200:
            return None
    values={k:metrics.get(k,0) for k in load_profile()['scheduler_weights']}
    with db:
        db.execute('INSERT OR IGNORE INTO tasks(task_id,dedupe_key,url,domain,kind,task_class,payload,source_id,venue_id,parent_url,parent_task_id,discovered_via,depth,priority,novelty,gap_importance,urgency,source_quality,cost,duplication,repeat_failures,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(task_id,key,url,registrable_domain(url) if url else '',kind,task_class,safe_payload,source_id,venue_id,strip_pii(parent_url) if parent_url else None,parent_task_id,strip_pii(discovered_via),depth,score(values),*(values[k] for k in ('novelty','gap_importance','urgency','source_quality','cost','duplication','repeat_failures')),now,now))
    return task_id

class Scheduler:
    def __init__(self, db, *, clock=None, budget=100, shares=None, discover_floor=.25, trial_limits=None):
        self.trial_limits=trial_limits or {}
        self.trial_used={}
        self.db=db;self.clock=clock or Clock();self.budget=budget
        self.allocations=split_budget(budget,shares,discover_floor); self.used={k:0 for k in TASK_CLASSES}
    def pick(self, *, cooldowns=None, deadline=None):
        """Select one task; the executor owns its running/done lease transitions.

        cooldowns and deadline are monotonic-clock seconds. The discovery
        reservation is retained while discovery work is cooling down.
        """
        cooldowns=cooldowns or {}
        while sum(self.used.values())<self.budget:
            now=kst_now(self.clock)
            rows=[dict(r) for r in self.db.execute("SELECT * FROM tasks WHERE state='pending' ORDER BY priority DESC,created_at,task_id")]
            if not rows: return None
            from .discover.search import usable_providers
            active=[]
            for row in rows:
                host=urlsplit(row.get('url') or '').hostname
                if host in self.trial_limits and self.trial_used.get(host,0)>=self.trial_limits[host]: continue
                payload=json.loads(row.get('payload') or '{}')
                if row['kind']=='search' or (row['kind']=='directory' and ('query' in payload or payload.get('kind')=='osm_search')):
                    names=['nominatim'] if payload.get('kind')=='osm_search' else [payload['provider']] if payload.get('provider') else None
                    if not usable_providers(self.db,clock=self.clock,names=names):
                        states=list(self.db.execute("SELECT provider,next_at FROM provider_state WHERE state IN ('disabled','inaccessible')"))
                        times=[r['next_at'] for r in states if (names is None or r['provider'] in names) and r['next_at'] and r['next_at']>now.isoformat()]
                        due=min(times) if times else None
                        self.db.execute('UPDATE tasks SET next_at=? WHERE task_id=?',(due,row['task_id']))
                        continue
                active.append(row)
            rows=active
            if not rows: return None
            reserved=max(0,self.allocations['discover']-self.used['discover'])
            remaining=self.budget-sum(self.used.values())
            if reserved>=remaining and any(r['task_class']=='discover' for r in rows):
                rows=[r for r in rows if r['task_class']=='discover']
            def ready_at(row):
                due=self.clock.monotonic()
                if row['next_at']:
                    due+=max(0,(datetime.fromisoformat(row['next_at'])-now).total_seconds())
                return max(due,cooldowns.get(row['domain'],0))
            ready=[r for r in rows if ready_at(r)<=self.clock.monotonic()]
            under=[r for r in ready if self.used[r['task_class']]<self.allocations[r['task_class']]]
            if ready:
                row=(under or ready)[0]; self.used[row['task_class']]+=1
                host=urlsplit(row.get('url') or '').hostname
                if host in self.trial_limits: self.trial_used[host]=self.trial_used.get(host,0)+1
                return row
            delay=min(ready_at(r)-self.clock.monotonic() for r in rows)
            if deadline is None or self.clock.monotonic()+delay>deadline: return None
            self.clock.sleep(min(delay,60))
        return None
