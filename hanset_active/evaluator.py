"""Explainable four-score trials, with cross-domain copy detection."""
import re
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import urlsplit
from .config import load_profile
from .db import deterministic_id
from .extract.html import extract
from .extract.fields import extract as extract_fields
from .scheduler import enqueue
from .util.timeutil import kst_now
from .util.urls import registrable_domain, is_excluded
from .util.pii import strip_pii

def shingles(text,n=5):
    words=re.findall(r'\w+',text.casefold())
    return {tuple(words[i:i+n]) for i in range(max(0,len(words)-n+1))}

def jaccard(left,right):
    a=shingles(left);b=shingles(right)
    return len(a&b)/len(a|b) if a or b else 0.0

def is_aggregator(url,profile=None):
    host=(urlsplit(url).hostname or '').lower()
    return any(host==d or host.endswith('.'+d) if '*' not in d else host.startswith(d.split('*')[0]) for d in (profile or load_profile())['aggregator_domains'])

def evaluate(url,documents, *, existing_texts=(), seed_domains=(), known_entities=(), known_fields=(), blocked=False, venue_name='',profile=None):
    profile=profile or load_profile()
    text='\n'.join(d.get('title','')+'\n'+d.get('text','') for d in documents)
    if is_excluded(url,text): return dict(relevance=0.,novelty=0.,extractability=0.,original=0.,total=0.,decision='inaccessible',reason='excluded')
    lower=text.casefold(); domain=registrable_domain(url)
    keyword_hits=sum(w.casefold() in lower for w in profile['relevance_keywords'])
    regions=any(w.casefold() in lower for r in profile['regions'] for w in r['aliases'])
    relevance=min(1.,keyword_hits*.25+.2*regions+.2*bool(venue_name and venue_name.casefold() in lower))
    copy=any(other_domain!=domain and jaccard(text,old)>=.8 for other_domain,old in existing_texts)
    entities={m.get('name','') for d in documents for m in d.get('mentions',[]) if m.get('name')}
    fields={r['field'] for d in documents for r in extract_fields(d.get('text',''))}
    new_share=len(entities-set(known_entities))/max(1,len(entities))
    novelty=max(0.,min(1.,.5*(domain not in seed_domains)+.3*new_share+.2*(len(fields-set(known_fields))/max(1,len(fields))))-.8*copy)
    has_text=any(len(d.get('text','').strip())>=80 for d in documents)
    has_dates=any(d.get('dates') for d in documents)
    has_struct=bool(fields) or any(d.get('structured',{}).get('tables') or any(l['kind'] in ('attachment','image_notice') for l in d.get('links',[])) for d in documents)
    extractability=(.5*has_text+.2*has_dates+.3*has_struct) if not blocked else 0.
    aggregator=is_aggregator(url,profile)
    canonical_elsewhere=any(d.get('canonical_url') and registrable_domain(d['canonical_url'])!=domain for d in documents)
    repost=bool(re.search(r'출처|\bvia\b|원문|repost',text,re.I))
    name_original=bool(venue_name and (venue_name.casefold() in domain or any(venue_name.casefold() in d.get('title','').casefold() for d in documents)))
    original=float(name_original and not (aggregator or canonical_elsewhere or repost or copy))
    total=.35*relevance+.25*novelty+.25*extractability+.15*original
    decision='inaccessible' if blocked else 'deprioritized' if aggregator or copy or not(total>=.5 and relevance>=.4 and extractability>0) else 'registered'
    return dict(relevance=relevance,novelty=novelty,extractability=extractability,original=original,total=total,decision=decision,reason='blocked' if blocked else 'copy' if copy else 'aggregator' if aggregator else 'rule_scores')

def trial_task(db,url, *, clock=None,discovered_via='evaluator'):
    return enqueue(db,kind='trial',url=url,payload={'max_pages':3},discovered_via=discovered_via,clock=clock)

def trial(context,url, *, venue_name=''):
    domain=registrable_domain(url)
    prior=context.db.execute('SELECT state FROM sources WHERE domain=?',(domain,)).fetchone()
    if prior and prior[0] in ('registered','deprioritized','inaccessible'): return prior[0]
    documents=[]; blocked=False;queue=[url];seen=set()
    while queue and len(seen)<3:
        target=queue.pop(0)
        if target in seen: continue
        seen.add(target);result=context.get(target)
        if result is None: break
        if not result.ok:
            blocked=result.outcome in ('blocked','robots_disallowed','login_wall')
            if blocked: break
            continue
        doc=extract(result.content,target)
        canonical=BeautifulSoup(result.content,'lxml').select_one('link[rel=canonical][href]')
        if canonical:
            from urllib.parse import urljoin
            doc['canonical_url']=urljoin(target,canonical['href'])
        documents.append(doc)
        if len(documents)==1:
            internal=[l for l in doc['links'] if registrable_domain(l['url'])==domain]
            best=next((l['url'] for l in internal if re.search(r'price|hours|rules|요금|시간|규정',l['url']+' '+l['text'],re.I)),None)
            lang=next((l['url'] for l in internal if l.get('hreflang') or re.search(r'/en/|/ko/',l['url'])),None)
            queue.extend(u for u in (best,lang) if u)
    existing=[]
    for row in context.db.execute('SELECT domain,blob_path FROM raw_documents WHERE domain<>?',(domain,)):
        if row['blob_path'] and Path(row['blob_path']).is_file(): existing.append((row['domain'],Path(row['blob_path']).read_text(errors='replace')))
    seeds=[r[0] for r in context.db.execute('SELECT domain FROM seed_domains')]
    known_entities=[r[0] for r in context.db.execute('SELECT name FROM venues')]
    known_fields=[r[0] for r in context.db.execute('SELECT DISTINCT field FROM observations')]
    result=evaluate(url,documents,existing_texts=existing,seed_domains=seeds,known_entities=known_entities,known_fields=known_fields,blocked=blocked,venue_name=venue_name,profile=context.profile)
    if result['reason']=='excluded': return 'inaccessible'
    sid=deterministic_id(domain);now=kst_now(context.clock).isoformat()
    with context.db:
        context.db.execute('INSERT INTO sources(source_id,domain,url,state,reason,quality,first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(domain) DO UPDATE SET state=excluded.state,reason=excluded.reason,quality=excluded.quality,last_seen_at=excluded.last_seen_at',(sid,domain,strip_pii(url),result['decision'],result['reason'],result['total'],now,now))
        context.db.execute('INSERT OR IGNORE INTO source_trials VALUES (?,?,?,?,?,?,?,?,?,?,?)',(deterministic_id(sid,url),sid,strip_pii(url),*(result[k] for k in ('relevance','novelty','extractability','original','total','decision','reason')),now))
    return result['decision']

DOC_TYPES=frozenset(('venue_page','price_page','hours_page','rules_page','directory_listing','news_post','event_notice','booking_page','pdf_attachment','image_notice','other'))

def record_doc_kind(db,kind, *, raw_id=None,clock=None):
    """Queue novel taxonomy for review, never change the configured taxonomy."""
    if kind in DOC_TYPES: return None
    kind=strip_pii(kind)
    if is_excluded(kind): return None
    cid=deterministic_id('doc_type',kind,raw_id)
    with db:
        db.execute('INSERT OR IGNORE INTO taxonomy_candidates VALUES (?,?,?,?,?,?)',(cid,'doc_type',kind,raw_id,'pending',kst_now(clock).isoformat()))
    return cid
