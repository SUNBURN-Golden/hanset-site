"""Rules-only labels and evidence persistence; no model calls."""
import json
import re
from .db import deterministic_id
from .util.pii import strip_pii
from .util.timeutil import kst_now
from .util.urls import registrable_domain
VERSION='1.0.0'
class ModelHook:
    def infer(self, *args, **kwargs):
        return None

def claim_status(source_type, *, ambiguous=False, hosted_on_own_domain=False, ocr_confidence=None):
    if ambiguous or (ocr_confidence is not None and ocr_confidence<60): return 'unverified'
    return 'fact' if source_type in ('own_site','official_directory') or (source_type=='attachment' and hosted_on_own_domain) else 'source_claim'

def labels(text, *, source_type='third_party', content_type='', **kwargs):
    text=strip_pii(text)
    status=claim_status(source_type,**kwargs)
    docs=[('price_page',r'요금|이용료|day pass|price'),('hours_page',r'운영시간|opening hours'),('rules_page',r'규정|문신|tattoo|rules'),('directory_listing',r'목록|directory|listing'),('news_post',r'뉴스|news'),('event_notice',r'행사|event'),('booking_page',r'예약|booking'),('venue_page',r'사우나|sauna|CrossFit|HYROX')]
    doc='pdf_attachment' if 'pdf' in content_type else 'image_notice' if 'image' in content_type else next((d for d,p in docs if re.search(p,text,re.I)),'other')
    out=[dict(kind='doc_type',value=doc,status=status,rule_id=f'doc.{doc}.v1')]
    for event,pattern in [('open',r'오픈|개장|grand opening'),('relocate',r'이전 안내|relocat'),('close',r'폐업|permanently closed'),('price_change',r'요금 변경|price change'),('hours_change',r'시간 변경|hours change'),('closure_notice',r'임시 휴무|temporary closure'),('collab',r'협업|collab')]:
        if re.search(pattern,text,re.I): out.append(dict(kind='event_type',value=event,status=status,rule_id=f'event.{event}.v1'))
    from .extract.fields import extract
    for evidence in extract(text):
        item=dict(kind='claim',value=evidence['field'],status=claim_status(source_type,**{**kwargs,'ambiguous':kwargs.get('ambiguous',False) or evidence['ambiguous']}),rule_id=evidence['rule_id'])
        if item not in out: out.append(item)
    return out

def store_observation(db, venue_key, evidence, *, source_url, source_type, raw_id=None, extraction_id=None, clock=None, own_domain=None):
    value=json.dumps(evidence['value'],ensure_ascii=False,sort_keys=True)
    value=strip_pii(value); source_url=strip_pii(source_url)
    vh=deterministic_id(value); oid=deterministic_id(venue_key,evidence['field'],vh,raw_id or source_url)
    status=claim_status(source_type,ambiguous=evidence.get('ambiguous',False),hosted_on_own_domain=bool(own_domain and registrable_domain(source_url)==registrable_domain(own_domain)))
    with db:
        db.execute('INSERT OR IGNORE INTO observations(obs_id,venue_key,field,value_json,value_text,value_hash,unit,label_status,source_url,source_type,quote,locator,raw_id,extraction_id,extractor_version,rule_id,observed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (oid,venue_key,evidence['field'],value,value,vh,evidence.get('unit'),status,source_url,source_type,strip_pii(evidence['quote'])[:240],json.dumps(evidence['locator']),raw_id,extraction_id,evidence.get('extractor_version',VERSION),evidence['rule_id'],kst_now(clock).isoformat()))
    return oid

def store_labels(db, raw_id, extraction_id, items, clock=None):
    with db:
        for item in items:
            db.execute('INSERT OR IGNORE INTO labels VALUES (?,?,?,?,?,?,?,?)',(deterministic_id(raw_id,item['kind'],item['value'],item['rule_id']),raw_id,extraction_id,item['kind'],item['value'],item['status'],item['rule_id'],kst_now(clock).isoformat()) )
