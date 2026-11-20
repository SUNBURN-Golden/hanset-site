"""Frozen seed identity, conservative venue merges and temporal field history."""
from datetime import datetime, timedelta
import csv
import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from .db import deterministic_id
from .util.pii import strip_pii
from .util.timeutil import kst_now
from .util.urls import canonicalize, registrable_domain, is_collect_denied, is_excluded, is_denied
from .util.followups import portal_domains

def normalize_name(name):
    return re.sub(r'[^a-z0-9가-힣]', '', re.sub(r'본점|지점|branch|main branch','',name.casefold()))

_ARTICLE_TITLE = re.compile(r'추천|어디|총정리|후기|[?!]|\bbest\b|\btop\s*\d+|^\d+\s*(?:곳|선)')


def is_article_title(name):
    """Detect article markers in a would-be venue name."""
    normalized = unicodedata.normalize('NFKC', name or '').strip().casefold()
    return bool(_ARTICLE_TITLE.search(normalized))


def seed(db, records=(), *, paths=(), approved_sources=None, refreeze=False, clock=None):
    previous=db.execute('SELECT seed_id FROM seed_venues UNION SELECT seed_id FROM seed_domains LIMIT 1').fetchone()
    # An empty seed also needs a persistent freeze marker.
    marker=db.execute("SELECT cursor FROM run_checkpoints WHERE checkpoint_id='seed_freeze'").fetchone()
    if not refreeze and (previous or marker): return previous[0] if previous else marker[0]
    rows=list(records)
    for path in paths:
        with open(path,encoding='utf-8-sig',newline='') as f:
            for row in csv.DictReader(f):
                rows.append({'name':row.get('name') or row.get('name_ko') or row.get('이름',''), 'website':row.get('website') or row.get('url') or row.get('홈페이지','')})
    domains=[]
    if approved_sources and Path(approved_sources).exists():
        data=json.loads(Path(approved_sources).read_text())
        entries=data if isinstance(data,list) else data.get('sources',data.get('origins',[]))
        for item in entries:
            url=item if isinstance(item,str) else item.get('url',item.get('origin',''))
            if url and not is_collect_denied(url): domains.append(registrable_domain(url))
    safe=[]
    for row in rows:
        name=strip_pii(row.get('name','')); website=strip_pii(row.get('website',''))
        if is_article_title(name): continue
        try:
            if website: website=canonicalize(website)
        except ValueError:
            website=''
        if website and is_collect_denied(website): continue
        safe.append((name,normalize_name(name),registrable_domain(website) if website else '',website))
    sid=deterministic_id(safe,sorted(domains),kst_now(clock).isoformat() if refreeze else 'initial')
    now=kst_now(clock).isoformat()
    with db:
        for name,norm,domain,url in safe:
            db.execute('INSERT OR IGNORE INTO seed_venues VALUES (?,?,?,?,?,?,?)',(deterministic_id(sid,norm,domain,url),sid,name,norm,domain,url,now))
            if domain: domains.append(domain)
        for domain in set(domains): db.execute('INSERT OR IGNORE INTO seed_domains VALUES (?,?,?,?)',(deterministic_id(sid,domain),sid,domain,now))
        db.execute("INSERT INTO run_checkpoints(checkpoint_id,cursor,updated_at) VALUES ('seed_freeze',?,?) ON CONFLICT(checkpoint_id) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at",(sid,now))
    return sid

def is_new(db,name,website=''):
    marker=db.execute("SELECT cursor FROM run_checkpoints WHERE checkpoint_id='seed_freeze'").fetchone()
    sid=marker[0] if marker else None
    return not db.execute('SELECT 1 FROM seed_venues WHERE normalized_name=? AND (? IS NULL OR seed_id=?)',(normalize_name(name),sid,sid)).fetchone() and not (website and db.execute('SELECT 1 FROM seed_domains WHERE domain=? AND (? IS NULL OR seed_id=?)',(registrable_domain(website),sid,sid)).fetchone())

def review(db,kind,venue_id,details,clock=None):
    details=strip_pii(json.dumps(details,ensure_ascii=False,sort_keys=True))
    key=deterministic_id(kind,venue_id,details)
    db.execute('INSERT OR IGNORE INTO review_queue(review_id,kind,venue_id,dedupe_key,details,state,created_at) VALUES (?,?,?,?,?,?,?)',(key,kind,venue_id,key,details,'open',kst_now(clock).isoformat()))
    return key

def venue_relevant(name, context='', profile=None):
    """Relevance gate: only sauna/spa/jjimjilbang/bathhouse/hotel spa/CrossFit/HYROX/gym venues are accepted.
    A positive keyword must appear in the venue name or its (short) category context; a food/cafe keyword in the
    name rejects unless the name itself also carries a strong positive keyword."""
    import re
    if profile is None:
        from .config import load_profile
        profile=load_profile()
    gate=profile.get('venue_gate',{})
    pos=lambda t: any(re.search(x,t,re.I) for x in gate.get('positive',[]))
    neg=lambda t: any(re.search(x,t,re.I) for x in gate.get('negative',[]))
    name=name or ''; context=(context or '')[:400]
    if neg(name) and not pos(name): return False
    return pos(name) or pos(context)

def upsert_venue(db, name, *, website='', city='', region='', venue_type='sauna', discovered_via='', clock=None, context=None):
    # context=None => trusted seed/legacy path (no gate); any string (even empty) => discovered venue, gated.
    if context is not None and not venue_relevant(name, context): return None
    name,website,city,region,discovered_via=map(strip_pii,(name,website,city,region,discovered_via))
    if not name or is_article_title(name) or is_excluded(name,website) or (website and is_denied(website)): return None
    website=canonicalize(website) if website else ''
    if website and registrable_domain(website) in portal_domains(): website=''  # a portal is never a venue's own site
    norm=normalize_name(name); domain=registrable_domain(website) if website else ''
    key=f'{norm}:{normalize_name(region)}:{normalize_name(city)}'
    vid=deterministic_id(key)
    with db:
        for row in db.execute('SELECT * FROM venues').fetchall():
            same_name=normalize_name(row['name'])==norm and normalize_name(row['city'] or '')==normalize_name(city)
            same_domain=bool(domain and domain in json.loads(row['domains'] or '[]'))
            if not city and (row['region'] or '')!=region: same_name=False
            if same_domain or same_name:
                domains=set(json.loads(row['domains'] or '[]'))
                if domain: domains.add(domain)
                effective_website=row['website'] or website
                db.execute('UPDATE venues SET domains=?,website=?,is_seed=?,seed_match=? WHERE venue_id=?',
                    (json.dumps(sorted(domains)),effective_website,int(not is_new(db,row['name'],effective_website)),
                     'frozen_seed' if not is_new(db,row['name'],effective_website) else None,row['venue_id']))
                db.execute('INSERT OR IGNORE INTO venue_aliases VALUES (?,?,?,?)',(deterministic_id(row['venue_id'],norm),row['venue_id'],name,norm))
                return row['venue_id']
            if SequenceMatcher(None,norm,normalize_name(row['name'])).ratio()>=.65:
                review(db,'possible_duplicate',row['venue_id'],{'candidate_name':name,'candidate_key':key,'website':website},clock)
        new=is_new(db,name,website)
        db.execute('INSERT OR IGNORE INTO venues VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(vid,key,name,name if re.search('[가-힣]',name) else None,venue_type,region,city,website,json.dumps([domain] if domain else []),int(not new),'frozen_seed' if not new else None,discovered_via,kst_now(clock).isoformat(),'unknown'))
        db.execute('INSERT OR IGNORE INTO venue_aliases VALUES (?,?,?,?)',(deterministic_id(vid,norm),vid,name,norm))
    return vid

def apply_observation(db, venue_id, obs_id, *, clock=None):
    obs=db.execute('SELECT * FROM observations WHERE obs_id=?',(obs_id,)).fetchone()
    if not obs or obs['label_status']!='fact': return None
    venue=db.execute('SELECT venue_key FROM venues WHERE venue_id=?',(venue_id,)).fetchone()
    if not venue or venue[0]!=obs['venue_key']: raise ValueError('Observation belongs to another venue')
    existing=db.execute('SELECT vf_id FROM venue_fields WHERE venue_id=? AND obs_id=?',(venue_id,obs_id)).fetchone()
    if existing: return existing[0]
    now=kst_now(clock).isoformat()
    with db:
        current=db.execute('SELECT * FROM venue_fields WHERE venue_id=? AND field=? AND valid_to IS NULL',(venue_id,obs['field'])).fetchone()
        if current and current['value_hash']==obs['value_hash']: return current['vf_id']
        # Multiple pages can be processed within one clock tick. Keep temporal
        # natural keys distinct when values change and return during that tick.
        latest=db.execute('SELECT max(valid_from) FROM venue_fields WHERE venue_id=? AND field=?',(venue_id,obs['field'])).fetchone()[0]
        if latest and now<=latest:
            now=(datetime.fromisoformat(latest)+timedelta(microseconds=1)).isoformat()
        vf=deterministic_id(venue_id,obs['field'],obs['value_hash'],obs_id)
        db.execute('INSERT OR IGNORE INTO venue_fields(vf_id,venue_id,field,value_json,value_text,value_hash,unit,label_status,source_url,quote,obs_id,valid_from) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(vf,venue_id,obs['field'],obs['value_json'],obs['value_text'],obs['value_hash'],obs['unit'],obs['label_status'],obs['source_url'],obs['quote'],obs_id,now))
        if current:
            db.execute('UPDATE venue_fields SET valid_to=?,superseded_by=? WHERE vf_id=?',(now,vf,current['vf_id']))
            db.execute('INSERT OR IGNORE INTO field_changes VALUES (?,?,?,?,?,?,?,?,?,?)',(deterministic_id(venue_id,obs['field'],current['value_hash'],obs['value_hash']),venue_id,obs['field'],current['value_json'],obs['value_json'],current['value_hash'],obs['value_hash'],current['obs_id'],obs_id,now))
    return vf
