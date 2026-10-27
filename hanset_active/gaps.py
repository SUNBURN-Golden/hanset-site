"""Evidence-only gap lifecycle and ordered, recorded reverse discovery."""
import json
from datetime import timedelta
from .scheduler import refresh_days
import re
from .config import load_profile
from .db import deterministic_id
from .discover.reverse import STRATEGIES, followups
from .util.pii import strip_pii
from .util.timeutil import kst_now
from .util.urls import registrable_domain
from .venues import review

def ensure_gaps(db, venue_id, *, clock=None):
    venue=db.execute('SELECT * FROM venues WHERE venue_id=?',(venue_id,)).fetchone()
    if not venue: raise ValueError('Unknown venue')
    with db:
        for f in load_profile()['fields']:
            if venue['type'] in f['applies_to']:
                db.execute('INSERT OR IGNORE INTO gaps(gap_id,venue_id,field,state,updated_at) VALUES (?,?,?,?,?)',(deterministic_id(venue_id,f['id']),venue_id,f['id'],'open',kst_now(clock).isoformat()))
    return db.execute('SELECT * FROM gaps WHERE venue_id=?',(venue_id,)).fetchall()

def record_attempt(db,gap_id,strategy, *, url='',state='done',task_id=None,reason='',clock=None):
    if strategy not in STRATEGIES: raise ValueError('Unknown gap strategy')
    url=strip_pii(url)
    aid=deterministic_id(gap_id,strategy,url)
    with db:
        db.execute('INSERT INTO gap_attempts VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(gap_id,strategy,url) DO UPDATE SET state=excluded.state,reason=excluded.reason,task_id=excluded.task_id',(aid,gap_id,strategy,url,state,task_id,strip_pii(reason),kst_now(clock).isoformat()))
    return aid

def advance(db,gap_id,documents=(), *, clock=None):
    gap=db.execute('SELECT * FROM gaps WHERE gap_id=?',(gap_id,)).fetchone()
    if not gap or gap['state']!='open': return []
    venue=db.execute('SELECT * FROM venues WHERE venue_id=?',(gap['venue_id'],)).fetchone()
    attempts=db.execute('SELECT * FROM gap_attempts WHERE gap_id=?',(gap_id,)).fetchall()
    if any(r['state']=='pending' for r in attempts): return []
    done={r['strategy'] for r in attempts}
    strategy=next((s for s in STRATEGIES if s not in done),None)
    if strategy is None: return []
    tasks=followups(db,venue,gap['field'],documents,strategy=strategy,clock=clock)
    if not tasks: record_attempt(db,gap_id,strategy,clock=clock,reason='no candidates')
    for tid in tasks:
        task=db.execute('SELECT url FROM tasks WHERE task_id=?',(tid,)).fetchone()
        record_attempt(db,gap_id,strategy,url=task[0] or tid,state='pending',task_id=tid,clock=clock)
    return tasks

def resolve(db,gap_id,pages=(), *, exhausted=False,clock=None):
    """Pages carry url, outcome, text, relevant, counterpart, attachment, unavailable.

    Absence requires three distinct on-domain successful pages and explicit
    counterpart/attachment coverage, supplied by the executor after exhaustion.
    """
    gap=db.execute('SELECT * FROM gaps WHERE gap_id=?',(gap_id,)).fetchone()
    if not gap: raise ValueError('Unknown gap')
    venue=db.execute('SELECT * FROM venues WHERE venue_id=?',(gap['venue_id'],)).fetchone()
    observations=db.execute("SELECT * FROM observations WHERE venue_key=? AND field=? AND label_status='fact' ORDER BY observed_at DESC",(venue['venue_key'],gap['field'])).fetchall()
    # Compare the latest fact from each source, so an updated price on one URL
    # supersedes that URL's old evidence instead of manufacturing a conflict.
    latest={}
    for obs in observations: latest.setdefault(obs['source_url'],obs)
    facts=list(latest.values()); values={o['value_hash'] for o in facts}
    state='open';evidence=None;obs_id=None;reason='strategies remain'
    if len(values)>1:
        state='conflict';reason='different facts from distinct sources'
        with db: review(db,'conflict',venue['venue_id'],{'field':gap['field'],'observations':sorted(o['obs_id'] for o in facts)},clock)
    elif facts:
        state='filled';evidence=facts[0]['source_url'];obs_id=facts[0]['obs_id'];reason='explicit original-source fact'
    elif exhausted:
        config=next(f for f in load_profile()['fields'] if f['id']==gap['field'])
        keywords=config['keywords_ko']+config['keywords_en']
        own=[p for p in pages if registrable_domain(p['url']) in json.loads(venue['domains'] or '[]')]
        successful={p['url']:p for p in own if p.get('outcome')=='ok' and p.get('relevant',True)}
        def cue(page):
            text=page.get('text','')
            return page.get('unavailable',False) or page.get('needs_extractor_review',False) or any(re.search(re.escape(k)+r'.{0,120}(?:\d|yes|no|있|없|가능|불가)',text,re.I|re.S) for k in keywords)
        if any(p.get('unavailable') or p.get('unextracted_attachment') or p.get('needs_extractor_review') for p in own) or any(cue(p) for p in successful.values()): state='not_extracted';reason='field cue without rule evidence'
        elif own and all(p.get('outcome') in ('blocked','robots_disallowed','login_wall','error','inaccessible') for p in own): state='inaccessible';reason='all candidates inaccessible'
        elif len(successful)>=3 and any(p.get('counterpart') for p in successful.values()) and any(p.get('attachment') for p in successful.values()) and not any(any(k.casefold() in p.get('text','').casefold() for k in keywords) for p in successful.values()): state='absent_in_source';reason='relevant source coverage exhausted'
        else: state='open';reason='insufficient source coverage'
    with db:
        db.execute('UPDATE gaps SET state=?,evidence_url=?,obs_id=?,reason=?,next_at=?,updated_at=? WHERE gap_id=?',(state,evidence,obs_id,reason,(kst_now(clock)+timedelta(days=refresh_days(gap['field']))).isoformat() if state!='open' else None,kst_now(clock).isoformat(),gap_id))
    return state

def reopen(db,gap_id, *, clock=None):
    """Only call from the freshness schedule when its next_at is due."""
    now=kst_now(clock).isoformat()
    with db:
        row=db.execute('SELECT next_at FROM gaps WHERE gap_id=?',(gap_id,)).fetchone()
        if not row or not row[0] or row[0]>now: return False
        db.execute("UPDATE gaps SET state='open',evidence_url=NULL,obs_id=NULL,updated_at=? WHERE gap_id=?",(now,gap_id))
        db.execute('DELETE FROM gap_attempts WHERE gap_id=?',(gap_id,))
    return True
