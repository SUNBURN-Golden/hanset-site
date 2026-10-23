"""HANSET fact schema. Untrusted callers cannot set workflow or trust fields."""
import datetime as dt
import hashlib
import json
import math
from .config import Rejected
from .privacy_filter import clean

STAGES = ('1_candidate', '2_source_checked', '3_deduplicated_out', '4_verified', '5_publish_pending')
CHANNELS = ('auto_discovery', 'browser_extract', 'manual_clip')
FOREIGNER_KEYS = ('public_access', 'restriction', 'booking', 'kr_phone', 'card', 'english',
                  'full_price', 'scrub_fee', 'last_entry', 'closures', 'age_tattoo_attire')
FIELDS = ('place_name_kr', 'place_name_en', 'branch', 'rates', 'hours', 'booking', 'foreigner_conditions', 'closures')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def payload(value, contacts=()):
    if not isinstance(value, dict):
        raise Rejected('invalid_payload')
    value = clean(value, contacts)
    out = {k: value.get(k) for k in ('title', 'published_at', 'facts', 'excerpt', 'locator', 'document_key', 'final_url', 'redirects')}
    if not isinstance(out['title'], str) or len(out['title']) > 240:
        raise Rejected('invalid_title')
    if out['published_at']:
        try:
            dt.date.fromisoformat(out['published_at'])
        except (TypeError, ValueError):
            raise Rejected('invalid_publication_date') from None
    if not isinstance(out['excerpt'], str) or not out['excerpt'] or len(out['excerpt']) > 2000:
        raise Rejected('invalid_excerpt')
    if not isinstance(out['locator'], str) or not out['locator'] or len(out['locator']) > 240:
        raise Rejected('missing_locator')
    facts = out['facts'] or []
    if not isinstance(facts, list) or len(facts) > 60:
        raise Rejected('invalid_facts')
    safe = []
    for f in facts:
        if not isinstance(f, dict) or f.get('field') not in FIELDS:
            raise Rejected('invalid_fact')
        if f['field'] == 'foreigner_conditions' and f.get('key') not in FOREIGNER_KEYS:
            raise Rejected('invalid_foreigner_key')
        if not isinstance(f.get('quote'), str) or not f['quote'] or f['quote'] not in out['excerpt'] or len(f['quote']) > 400:
            raise Rejected('unsupported_fact')
        if '[untrusted instruction removed]' in f['quote']:
            continue
        if not isinstance(f.get('locator'), str) or not f['locator'] or len(f['locator']) > 240 or 'value' not in f:
            raise Rejected('missing_fact_evidence')
        if isinstance(f['value'], float) and not math.isfinite(f['value']):
            raise Rejected('invalid_fact_value')
        if f['value'] is None or '[untrusted instruction removed]' in json.dumps(f['value']):
            raise Rejected('invalid_fact_value')
        scope = f.get('scope', {})
        if not isinstance(scope, dict) or set(scope) - {'audience', 'day', 'service', 'unit', 'currency', 'valid_from', 'valid_until'}:
            raise Rejected('invalid_scope')
        if any(not isinstance(v, str) or not v or len(v) > 120 for v in scope.values()):
            raise Rejected('invalid_scope')
        for k in ('valid_from', 'valid_until'):
            if scope.get(k):
                try: dt.date.fromisoformat(scope[k])
                except (TypeError, ValueError): raise Rejected('invalid_scope_date') from None
        if scope.get('valid_from') and scope.get('valid_until') and scope['valid_from'] > scope['valid_until']:
            raise Rejected('invalid_scope_date')
        safe.append({k: f[k] for k in ('field', 'value', 'quote', 'locator', 'scope', 'key') if k in f})
    out['facts'] = safe
    return out
