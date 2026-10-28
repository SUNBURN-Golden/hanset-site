"""Ontology mapping and the temporal state machine (close old period, never overwrite)."""
import json
import re
from datetime import datetime, timedelta
from hanset_active.db import deterministic_id
from hanset_active.util.timeutil import kst_now
from .store import dumps, vhash

ENTITY_TYPES = ('venue', 'branch', 'facility', 'price_plan', 'hours', 'usage_condition', 'program')
FACILITY_KINDS = ('dry_sauna', 'wet_sauna', 'sauna', 'kiln_sauna', 'cold_plunge', 'rapid_cold_plunge',
                  'ice_bath', 'warm_bath', 'hot_bath', 'layout')
BATH_WORDS = {'건식': 'dry_sauna', '습식': 'wet_sauna', '스팀': 'wet_sauna', '한증막': 'kiln_sauna',
              '냉탕': 'cold_plunge', '아이스배스': 'ice_bath', '온탕': 'warm_bath', '열탕': 'hot_bath'}
# Every branch is expected to answer these slots; missing ones stay explicit 'unverified'.
REQUIRED_SLOTS = [
    ('facility', 'sauna', 'temperature_c'), ('facility', 'cold_plunge', 'temperature_c'),
    ('facility', 'rapid_cold_plunge', 'exists'), ('facility', 'warm_bath', 'exists'),
    ('facility', 'hot_bath', 'exists'), ('facility', 'dry_sauna', 'exists'), ('facility', 'wet_sauna', 'exists'),
    ('facility', 'cold_plunge', 'exists'), ('facility', 'layout', 'bath_list'),
    ('price_plan', 'day_pass', 'amount'), ('hours', 'regular', 'open_hours'), ('hours', 'closure', 'closure_rule'),
    ('usage_condition', 'foreigner', 'policy'), ('usage_condition', 'tattoo', 'policy'),
    ('usage_condition', 'age', 'min_age'), ('usage_condition', 'access', 'policy'), ('program', 'program', 'listing'),
]
RETENTION_DAYS = 365

def now_iso(clock=None):
    return kst_now(clock).isoformat()

def ensure_entity(db, entity_type, parent_id, kind, *, name=None, qualifiers=None, origin='active_store', clock=None):
    assert entity_type in ENTITY_TYPES
    q = qualifiers or {}
    eid = deterministic_id('entity', entity_type, parent_id, kind, q)
    db.execute('INSERT OR IGNORE INTO entities VALUES (?,?,?,?,?,?,?,?)',
               (eid, entity_type, parent_id, kind, name, dumps(q), now_iso(clock), origin))
    return eid

def add_alias(db, entity_id, alias, lang=None):
    from hanset_active.venues import normalize_name
    if alias and alias.strip():
        db.execute('INSERT OR IGNORE INTO aliases VALUES (?,?,?,?)', (entity_id, alias.strip(), branch_key(alias), lang))

def branch_key(name):
    """Normalisation that KEEPS branch tokens (지점/점/branch) so branches never collapse."""
    return re.sub(r'[^a-z0-9가-힣]', '', (name or '').casefold())

def map_field(field, value):
    """Active-bot field -> list of (entity_type, kind, qualifiers, attribute, value, unit)."""
    out = []
    if field in ('name', 'address', 'website'):
        out.append(('branch', None, {}, field, value, None))
    elif field == 'hours' and isinstance(value, dict):
        out.append(('hours', 'regular', {'scope': value.get('scope', 'unspecified')}, 'open_hours', value.get('hours'), None))
    elif field == 'closures':
        out.append(('hours', 'closure', {}, 'closure_rule', value, None))
    elif field == 'price_day_pass' and isinstance(value, dict):
        q = {k: value[k] for k in ('ticket', 'scope', 'audience', 'duration_hours') if k in value}
        out.append(('price_plan', value.get('ticket', 'day_pass'), q, 'amount', value.get('amount'), 'KRW'))
    elif field == 'sauna_temp_c':
        out.append(('facility', 'sauna', {}, 'temperature_c', value, 'C'))
    elif field == 'cold_bath_temp_c':
        out.append(('facility', 'cold_plunge', {}, 'temperature_c', value, 'C'))
    elif field == 'rapid_cold_plunge':
        out.append(('facility', 'rapid_cold_plunge', {}, 'exists', bool(value), None))
    elif field == 'bath_composition' and isinstance(value, list):
        out.append(('facility', 'layout', {}, 'bath_list', sorted(value), None))
        for word in value:
            if word in BATH_WORDS:
                out.append(('facility', BATH_WORDS[word], {}, 'exists', True, None))
    elif field == 'foreigner_policy':
        out.append(('usage_condition', 'foreigner', {}, 'policy', value, None))
    elif field == 'tattoo_policy':
        out.append(('usage_condition', 'tattoo', {}, 'policy', value, None))
    elif field == 'min_age':
        out.append(('usage_condition', 'age', {}, 'min_age', value, None))
    elif field == 'access_policy':
        out.append(('usage_condition', 'access', {}, 'policy', value, None))
    return out

def subject_entity(db, branch_id, etype, kind, q, clock=None):
    return branch_id if etype == 'branch' else ensure_entity(db, etype, branch_id, kind, qualifiers=q, clock=clock)

def status_for(label, source_type):
    """Only an explicit rule match on the venue's own site is 'fact'. Directories and the
    curated CSV are 'source_claim'. Ambiguous matches never become values."""
    if label == 'fact' and source_type == 'own_site':
        return 'fact'
    if label in ('fact', 'source_claim'):
        return 'source_claim'
    return None

def _slot(entity_id, attribute, source_url):
    return deterministic_id('slot', entity_id, attribute, source_url or 'none')

def _bump(db, slot, at):
    latest = db.execute('SELECT max(effective_from) FROM facts WHERE slot_key=?', (slot,)).fetchone()[0]
    if latest and at <= latest:
        at = (datetime.fromisoformat(latest) + timedelta(microseconds=1)).isoformat()
    return at

def ensure_placeholder(db, entity_id, attribute, at, clock=None):
    """Explicit 'no evidence' fact so gaps are visible and never silently filled."""
    has_open = db.execute('SELECT 1 FROM facts WHERE entity_id=? AND attribute=? AND effective_to IS NULL', (entity_id, attribute)).fetchone()
    if has_open:
        return None
    slot = _slot(entity_id, attribute, None)
    at = _bump(db, slot, at)
    fid = deterministic_id('fact', slot, 'null', at)
    db.execute('''INSERT INTO facts(fact_id,entity_id,attribute,slot_key,value_json,value_hash,unit,status,source_url,source_type,
        evidence_locator,quote,origin_ref,collected_at,effective_from,rights,created_at)
        VALUES (?,?,?,?,NULL,?,NULL,'unverified',NULL,'none',?,NULL,'placeholder',?,?,'none',?)''',
        (fid, entity_id, attribute, slot, vhash(None), json.dumps({'reason': 'no_evidence'}), at, at, now_iso(clock)))
    return fid

def apply_snapshot(db, entity_id, attribute, source_url, values, *, at, clock=None):
    """values: list of dicts(value, unit, status, source_type, locator, quote, origin_ref).
    One snapshot = everything one source document says about one slot at time `at`.
    Differences against the open period close the old facts and append new ones."""
    slot = _slot(entity_id, attribute, source_url)
    by_hash = {}
    for v in values:
        by_hash.setdefault(vhash(dumps(v['value'])), v)
    open_rows = {r['value_hash']: r for r in db.execute(
        'SELECT * FROM facts WHERE slot_key=? AND effective_to IS NULL', (slot,))}
    new_hashes = set(by_hash) - set(open_rows)
    gone = set(open_rows) - set(by_hash)
    at = _bump(db, slot, at) if new_hashes else at
    created = []
    for h in sorted(new_hashes):
        v = by_hash[h]
        fid = deterministic_id('fact', slot, h, at)
        retention = (datetime.fromisoformat(at) + timedelta(days=RETENTION_DAYS)).isoformat()
        db.execute('''INSERT OR IGNORE INTO facts(fact_id,entity_id,attribute,slot_key,value_json,value_hash,unit,status,source_url,
            source_type,evidence_locator,quote,origin_ref,collected_at,effective_from,rights,retention_until,last_confirmed_at,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (fid, entity_id, attribute, slot, dumps(v['value']), h, v.get('unit'), v['status'], source_url, v['source_type'],
             dumps(v['locator']), (v.get('quote') or None) and v['quote'][:240], v['origin_ref'], v['collected_at'], at,
             'internal_review_only' if v['source_type'] == 'verified_csv' else 'public_page_factual', retention, at, now_iso(clock)))
        created.append(fid)
    successor = created[0] if len(created) == 1 else None
    for h in gone:
        old = open_rows[h]
        db.execute("UPDATE facts SET effective_to=?, superseded_by=?, closed_reason='value_change' WHERE fact_id=?",
                   (at, successor, old['fact_id']))
        for fid in created or [None]:
            db.execute('INSERT OR IGNORE INTO corrections VALUES (?,?,?,?,?,?,?)',
                       (deterministic_id('corr', old['fact_id'], fid), entity_id, attribute, old['fact_id'], fid, 'value_change', at))
    for h in set(by_hash) & set(open_rows):
        db.execute('UPDATE facts SET last_confirmed_at=? WHERE fact_id=?', (at, open_rows[h]['fact_id']))
    if created:  # evidence arrived: close the unverified placeholder for this attribute
        for ph in db.execute("SELECT fact_id, effective_from FROM facts WHERE entity_id=? AND attribute=? AND status='unverified' AND effective_to IS NULL",
                             (entity_id, attribute)).fetchall():
            db.execute("UPDATE facts SET effective_to=?, superseded_by=?, closed_reason='evidence_found' WHERE fact_id=?",
                       (max(at, ph[1]), created[0], ph[0]))
            db.execute('INSERT OR IGNORE INTO corrections VALUES (?,?,?,?,?,?,?)',
                       (deterministic_id('corr', ph[0], created[0]), entity_id, attribute, ph[0], created[0], 'evidence_found', at))
    return created

def refresh_conflicts(db, clock=None):
    """Keep conflicting open values side by side; never pick a winner automatically."""
    at = now_iso(clock)
    seen = set()
    rows = db.execute('''SELECT entity_id, attribute, json_group_array(fact_id) ids, count(DISTINCT value_hash) n,
        count(DISTINCT slot_key) slots FROM facts WHERE effective_to IS NULL AND value_json IS NOT NULL
        GROUP BY entity_id, attribute HAVING n>1''').fetchall()
    for r in rows:
        ids = sorted(json.loads(r['ids']))
        cid = deterministic_id('conflict', r['entity_id'], r['attribute'], ids)
        seen.add(cid)
        reason = 'same_source_multiple_values' if r['slots'] == 1 else 'sources_disagree'
        db.execute('INSERT OR IGNORE INTO conflicts VALUES (?,?,?,?,?,?,?,?)',
                   (cid, r['entity_id'], r['attribute'], json.dumps(ids), reason, 'open', at, at))
    for (cid,) in db.execute("SELECT conflict_id FROM conflicts WHERE state='open'").fetchall():
        if cid not in seen:
            db.execute("UPDATE conflicts SET state='obsolete', updated_at=? WHERE conflict_id=?", (at, cid))
    return len(seen)
