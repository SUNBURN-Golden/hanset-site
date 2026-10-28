"""Nine daemon stages. Each is resumable from its checkpoint and processes items in seq order."""
import csv
import hashlib
import json
import re
from pathlib import Path
from hanset_active.db import deterministic_id
from hanset_active.extract import fields as field_rules
from hanset_active.util.pii import strip_pii
from hanset_active.util.urls import is_denied, registrable_domain, canonicalize
from . import ontology as onto
from .store import dumps

CSV_COLUMNS_TEXT = ('notable', 'notes', 'price_note')
SUPPORTED_NOTE = {
    'instagram.com': 'Required source, NOT collected: login wall and no approved Graph API token. No account rotation, impersonation or proxies.',
    'naver.com': 'Excluded by owner rule (no Naver). Not collected.',
    'catchtable.co.kr': 'robots.txt disallows crawling. Not collected.',
}

class Ctx:
    def __init__(self, db, active, *, csv_path, run_id, clock=None, batch=500):
        self.db, self.active, self.csv_path, self.run_id, self.clock, self.batch = db, active, Path(csv_path), run_id, clock, batch

def _seq(db):
    return (db.execute('SELECT max(seq) FROM work_items').fetchone()[0] or 0) + 1

# 1. discover ---------------------------------------------------------------
def discover(ctx, cursor):
    """Register sources with rights/coverage, and candidate branches from both origins."""
    db, at = ctx.db, onto.now_iso(ctx.clock)
    n = 0
    if ctx.active is not None:
        for s in ctx.active.execute('SELECT domain, state, source_type, reason FROM sources'):
            supported = int(s['state'] in ('registered', 'deprioritized') and not is_denied('https://' + s['domain']))
            db.execute('INSERT OR REPLACE INTO source_rights VALUES (?,?,?,?,?,?,?,?,?)',
                (s['domain'], s['source_type'], json.dumps(['name', 'address', 'website', 'hours', 'closures', 'price_day_pass',
                 'bath_composition', 'sauna_temp_c', 'cold_bath_temp_c', 'rapid_cold_plunge', 'foreigner_policy', 'tattoo_policy']),
                 'public_page_factual', 7, 'facts: keep history; quotes: 365 days; raw HTML stays in active store', supported,
                 None if supported else f"state={s['state']}", at))
        for v in ctx.active.execute('SELECT venue_key, name, name_ko, website, city FROM venues'):
            if _owner_excluded(v['name'], v['name_ko'], v['website']):
                continue
            db.execute('INSERT OR IGNORE INTO candidate_branches(subject_ref,name,name_ko,website,city,origin,updated_at) VALUES (?,?,?,?,?,?,?)',
                       ('active:' + v['venue_key'], v['name'], v['name_ko'], v['website'], v['city'], 'active_store', at)); n += 1
    for domain, note in SUPPORTED_NOTE.items():
        db.execute('INSERT OR REPLACE INTO source_rights VALUES (?,?,?,?,?,?,?,?,?)',
                   (domain, 'sns' if 'instagram' in domain else 'third_party', '[]', 'none', None, 'not stored', 0, note, at))
    if ctx.csv_path.exists():
        for i, row in enumerate(csv.DictReader(ctx.csv_path.open(encoding='utf-8-sig'))):
            if _excluded(row):
                continue
            db.execute('INSERT OR IGNORE INTO candidate_branches(subject_ref,name,name_ko,website,city,origin,updated_at) VALUES (?,?,?,?,?,?,?)',
                       ('csv:' + row['name'], row['name'], row.get('name_ko'), row.get('website'), row.get('city'), 'verified_csv', at)); n += 1
    return cursor, n

def _excluded(row):
    return ('excluded' in (row.get('verification_status') or '').lower()
            or _owner_excluded(row.get('name'), row.get('name_ko'), row.get('website')))

# The owner excluded this venue (declined publication): it must never become a branch,
# whatever the verification_status column says.
_OWNER_EXCLUDED = re.compile(r'runport|런포트|butfit', re.I)

def _owner_excluded(*fields):
    return any(_OWNER_EXCLUDED.search(f or '') for f in fields)

# 2. incremental fetch ------------------------------------------------------
def incremental_fetch(ctx, cursor):
    """No network here: the active bot (hanset_active cron) does polite fetching. This stage pulls
    new raw documents from that store after the cursor, plus CSV rows whose content changed."""
    db, at, n = ctx.db, onto.now_iso(ctx.clock), 0
    cur = json.loads(cursor) if cursor else {'fetched_at': ''}
    if ctx.active is not None:
        rows = ctx.active.execute('''SELECT DISTINCT d.raw_id, d.canonical_url, d.url, d.fetched_at, d.content_hash FROM raw_documents d
            JOIN observations o ON o.raw_id=d.raw_id WHERE d.outcome='ok' AND d.fetched_at>? ORDER BY d.fetched_at, d.raw_id''',
            (cur['fetched_at'],)).fetchall()
        for r in rows:
            if is_denied(r['canonical_url']):
                continue
            db.execute('INSERT OR IGNORE INTO work_items VALUES (?,?,?,?,?,?,NULL,?,?)',
                       (r['raw_id'], 'active_store', r['canonical_url'], r['url'] or r['canonical_url'], r['fetched_at'], r['content_hash'], _seq(db), at))
            cur['fetched_at'] = r['fetched_at']; n += 1
    if ctx.csv_path.exists():
        for i, row in enumerate(csv.DictReader(ctx.csv_path.open(encoding='utf-8-sig'))):
            if _excluded(row):
                continue
            h = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            item = 'csv:' + h
            if not db.execute('SELECT 1 FROM work_items WHERE item_id=?', (item,)).fetchone():
                fetched = (row.get('last_verified') or at[:10])[:10] + 'T00:00:00+09:00'
                db.execute('INSERT INTO work_items VALUES (?,?,?,?,?,?,NULL,?,?)',
                           (item, 'verified_csv', 'csv:saunas.csv#' + row['name'], row.get('source_url') or None, fetched, h, _seq(db), at)); n += 1
    return json.dumps(cur), n

# 3. change detect ----------------------------------------------------------
def change_detect(ctx, cursor):
    db, n = ctx.db, 0
    for w in db.execute('SELECT * FROM work_items WHERE seq>? ORDER BY seq LIMIT ?', (int(cursor or 0), ctx.batch)).fetchall():
        prev = db.execute('SELECT content_hash FROM work_items WHERE canonical_url=? AND seq<? ORDER BY seq DESC LIMIT 1', (w['canonical_url'], w['seq'])).fetchone()
        db.execute('UPDATE work_items SET changed=? WHERE item_id=?', (int(prev is None or prev[0] != w['content_hash']), w['item_id']))
        cursor = w['seq']; n += 1
    return str(cursor or 0), n

# 4. extract ----------------------------------------------------------------
_AGE = re.compile(r'(?:age|ages)\s*(\d{1,2})\s*\+|(\d{1,2})\s*세\s*이상')
def _access(type_text):
    t = (type_text or '').lower()
    if 'open to non-guests' in t: return 'open_to_non_guests'
    if 'guests only' in t or '(guests)' in t: return 'guests_only'
    if 'members' in t: return 'members_and_guests'
    if 'reservation' in t or 'booking only' in t: return 'reservation_required'
    return None

# Deterministic price_note classifier. A curated price_krw becomes 'day_pass' only when the
# amount itself appears in the note next to a general/adult single-admission cue; hotel-guest
# rates/add-ons become 'guest_rate'; spa courses, treatments and memberships stay 'listed_price'.
_ADMIT_CUE = re.compile(r'정상가|일반|대인|성인|adults?|종일권|일일|입장료|입장권|1회\s*이용권|1회권|day.?pass|admission|single\s*entry|\bentry\b|\b1\s*person\b|1인|day\s*\(', re.I)
_GUEST_CUE = re.compile(r'투숙객|숙박객|게스트|guests?[-\s]only|guest\s*rate|add-?on|애드온', re.I)
_COURSE_CUE = re.compile(r'시수|코스|\bcourse\b|treatment|membership|멤버십|회원권|패키지|package|프로그램|세신', re.I)
_GUESTS_ONLY_NOTE = re.compile(r'guests?[-\s]only|투숙객\s*전용|숙박객\s*전용|게스트\s*전용', re.I)
_WEEKDAY_CUE = re.compile(r'주중|평일|weekdays?', re.I)
_ADULT_CUE = re.compile(r'성인|대인|adults?', re.I)
_CHILD_CUE = re.compile(r'소인|어린이|소아|children?|kids?', re.I)
_DURATION = re.compile(r'(\d+)\s*(시간|hours?|hrs?|h(?![a-z])|분|minutes?|min\b)', re.I)
_MONEY_IN_PAREN = re.compile(r'₩\s*[\d,]|[\d,]+\s*원|\bKRW\b', re.I)

def classify_price_note(note, amount_krw):
    """Classify the curated CSV price from its note. Deterministic string rules only.
    Returns (ticket, qualifiers): ticket in {'day_pass','guest_rate','listed_price'};
    qualifiers (day_pass only) may carry scope/audience/duration_hours."""
    note = (note or '').strip()
    try:
        amount = int(str(amount_krw).strip())
    except (TypeError, ValueError):
        return 'listed_price', {}
    if not note or amount <= 0 or field_rules._UNCONFIRMED.search(note):
        return 'listed_price', {}
    occurrences = re.compile(r'(?<![\d])' + r'[,.]?'.join(re.escape(d) for d in str(amount)) + r'(?![\d])')
    guests_only = bool(_GUESTS_ONLY_NOTE.search(note))
    result = None
    prices = list(re.finditer(r'₩\s*[\d,]+|[\d,]+\s*(?:원|KRW)', note, re.I))
    prev_end = 0
    for m in occurrences.finditer(note):
        preceding = [p.end() for p in prices if p.end() <= m.start()]
        prev_end = max([prev_end] + preceding)
        window = note[max(prev_end, m.start() - 60):m.start()]  # cues must precede their amount
        prev_end = m.end()
        paren = re.match(r'\s*[\(（][^\)）]{0,60}', note[m.end():])  # qualifier attached after the amount
        post = paren.group(0) if paren and not _MONEY_IN_PAREN.search(paren.group(0)) else ''  # a paren with its own price is another tier, not this one
        if _GUEST_CUE.search(window) or _GUEST_CUE.search(post) or guests_only:
            ticket = 'guest_rate'
        elif _COURSE_CUE.search(window):
            ticket = 'listed_price'
        elif _ADMIT_CUE.search(window):
            ticket = 'day_pass'
        else:
            ticket = 'listed_price'
        if result is None or (result[0] != 'guest_rate' and ticket == 'guest_rate'):
            quals = {}
            if ticket == 'day_pass':
                if _WEEKDAY_CUE.search(window): quals['scope'] = 'weekday'
                if _ADULT_CUE.search(window): quals['audience'] = 'adult'
                elif _CHILD_CUE.search(window): quals['audience'] = 'child'
                d = _DURATION.search(window + ' ' + post)
                if d:
                    hours = int(d[1]) / (60 if d[2].lower().startswith(('분', 'min')) else 1)
                    quals['duration_hours'] = int(hours) if hours == int(hours) else hours
            result = (ticket, quals)
    return result or ('listed_price', {})

def _load_csv_rows(ctx):
    rows = {}
    if ctx.csv_path.exists():
        for i, row in enumerate(csv.DictReader(ctx.csv_path.open(encoding='utf-8-sig'))):
            rows['csv:' + hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).hexdigest()] = (i + 2, row)
    return rows

def _add_candidate(db, w, subject, field, value, unit, label, stype, url, quote, locator, ref, at):
    db.execute('INSERT OR IGNORE INTO candidates(cand_id,item_id,seq,subject_ref,field,value_json,unit,label,source_type,source_url,quote,locator,origin_ref,observed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (deterministic_id('cand', w['item_id'], ref, field, value), w['item_id'], w['seq'], subject, field, dumps(value), unit, label, stype, url, quote, dumps(locator), ref, at))

def _extract_csv_item(db, w, line, row):
    """Deterministic extraction of one curated CSV row: website, classified price, access from
    the type column, and the shared field rules over every text column (PII stripped)."""
    if _excluded(row):
        return
    subject, url, at = 'csv:' + row['name'], row.get('source_url') or row.get('website'), w['fetched_at']
    base = {'file': 'saunas.csv', 'line': line}
    def add(field, value, unit, label, quote, locator, ref):
        _add_candidate(db, w, subject, field, value, unit, label, 'verified_csv', url, quote, locator, ref, at)
    if row.get('website'):
        add('website', row['website'], None, 'fact', row['website'][:240], {**base, 'column': 'website'}, f'csv:{line}:website')
    if (row.get('price_krw') or '').strip().isdigit() and not field_rules._UNCONFIRMED.search(row.get('price_note') or ''):
        note = strip_pii(row.get('price_note') or '')
        ticket, quals = classify_price_note(note, int(row['price_krw']))
        # Repeated matching amounts with different tiers cannot be assigned safely.
        digits = str(int(row['price_krw']))
        matches = list(re.finditer(r'(?<![\d])' + r',?'.join(digits) + r'(?![\d])', note))
        label = 'unverified' if len(matches) > 1 else 'fact'
        add('price_day_pass', {'amount': int(row['price_krw']), 'ticket': ticket, 'scope': 'unspecified', **quals}, 'KRW', label,
            note[:240], {**base, 'column': 'price_krw'}, f'csv:{line}:price')
    acc = _access(row.get('type'))
    if acc:
        add('access_policy', acc, None, 'fact', strip_pii(row['type'])[:240], {**base, 'column': 'type'}, f'csv:{line}:type')
    for col in CSV_COLUMNS_TEXT:
        text = strip_pii(row.get(col) or '')
        for r in field_rules.extract(text):
            if r['field'] == 'price_day_pass':
                continue  # CSV prices come only from the curated price_krw + classifier above
            add(r['field'], r['value'], r.get('unit'), 'unverified' if r['ambiguous'] else 'fact', r['quote'],
                {**base, 'column': col, **r['locator']}, f"csv:{line}:{col}:{r['rule_id']}:{r['locator']}")
        if field_rules._UNCONFIRMED.search(text):
            continue
        for m in _AGE.finditer(text):
            add('min_age', int(m[1] or m[2]), 'years', 'fact', text[max(0, m.start()-40):m.end()+40][:240],
                {**base, 'column': col, 'char_offsets': [m.start(), m.end()]}, f'csv:{line}:{col}:age:{m.start()}')

def extract(ctx, cursor):
    """Reuse hanset_active observations (rule extractor fields.py) and run the same deterministic
    rules over the curated CSV text. No model calls; no AI-filled values."""
    db, n = ctx.db, 0
    items = db.execute('SELECT * FROM work_items WHERE seq>? AND changed IS NOT NULL ORDER BY seq LIMIT ?', (int(cursor or 0), ctx.batch)).fetchall()
    csv_rows = _load_csv_rows(ctx)
    for w in items:
        if w['origin'] == 'active_store' and ctx.active is not None:
            for o in ctx.active.execute('SELECT * FROM observations WHERE raw_id=?', (w['item_id'],)):
                _add_candidate(db, w, 'active:' + o['venue_key'], o['field'], json.loads(o['value_json']) if o['value_json'] else None, o['unit'], o['label_status'],
                    o['source_type'], o['source_url'], o['quote'], json.loads(o['locator']) if o['locator'] else None, o['obs_id'], w['fetched_at'])
        elif w['origin'] == 'verified_csv' and w['item_id'] in csv_rows:
            line, row = csv_rows[w['item_id']]
            _extract_csv_item(db, w, line, row)
        cursor = w['seq']; n += 1
    return str(cursor or 0), n

# 5. entity resolution --------------------------------------------------------
def entity_resolution(ctx, cursor):
    """CSV rows are the curated branch list. An active-bot venue joins a CSV branch only on an exact
    branch-keeping name match or the exact same website URL. Same domain alone (e.g. two hotels of one
    chain) is never merged; it goes to review. Article/listicle 'venues' without a venues row stay unresolved."""
    db, at, n = ctx.db, onto.now_iso(ctx.clock), 0
    rows = db.execute('SELECT * FROM candidate_branches WHERE branch_id IS NULL ORDER BY origin DESC, subject_ref').fetchall()
    for c in rows:  # verified_csv first (DESC on origin)
        if _owner_excluded(c['name'], c['name_ko'], c['website']):
            continue
        if c['origin'] == 'verified_csv':
            bid = _make_branch(db, c, ctx.clock); res = 'csv_seed'
        else:
            bid, res = _match(db, c)
            if bid is None and res == 'new':
                bid = _make_branch(db, c, ctx.clock)
        db.execute('UPDATE candidate_branches SET branch_id=?, resolution=?, updated_at=? WHERE subject_ref=?', (bid, res, at, c['subject_ref'])); n += 1
    # candidates whose subject has no candidate_branches row (listicle pages) -> review, not loaded
    for (subject,) in db.execute('''SELECT DISTINCT subject_ref FROM candidates WHERE branch_id IS NULL AND verdict IS NULL
            AND subject_ref NOT IN (SELECT subject_ref FROM candidate_branches)''').fetchall():
        db.execute('INSERT OR IGNORE INTO review_queue VALUES (?,?,?,?,?,?)', (deterministic_id('rq', 'unresolved', subject), 'unresolved_subject', subject,
                   json.dumps({'reason': 'observation subject is not a venue record (likely an article/list page); not merged into any branch'}, ensure_ascii=False), 'open', at))
        db.execute("UPDATE candidates SET verdict='rejected', verdict_reason='unresolved_subject' WHERE subject_ref=? AND verdict IS NULL", (subject,))
    db.execute('UPDATE candidates SET branch_id=(SELECT branch_id FROM candidate_branches b WHERE b.subject_ref=candidates.subject_ref) WHERE branch_id IS NULL')
    return cursor, n

def _url_key(u):
    try: return canonicalize(u).rstrip('/').replace('http://', 'https://')
    except Exception: return None

def _match(db, c):
    keys = {onto.branch_key(c['name']), onto.branch_key(c['name_ko'])} - {''}
    for k in keys:
        hit = db.execute("SELECT a.entity_id FROM aliases a JOIN entities e USING(entity_id) WHERE e.entity_type='branch' AND a.normalized=?", (k,)).fetchall()
        if len(hit) == 1: return hit[0][0], 'name_exact'
    if c['website']:
        u = _url_key(c['website'])
        hit = db.execute("SELECT entity_id FROM external_ids WHERE system='website' AND external_id=?", (u,)).fetchall()
        if len(hit) == 1: return hit[0][0], 'website_exact'
        dom = registrable_domain(c['website'])
        same = db.execute("SELECT entity_id FROM external_ids WHERE system='domain' AND external_id=?", (dom,)).fetchall()
        if same:
            db.execute('INSERT OR IGNORE INTO review_queue VALUES (?,?,?,?,?,?)', (deterministic_id('rq', 'possible_same', c['subject_ref']), 'possible_same_branch', c['subject_ref'],
                       json.dumps({'domain': dom, 'branches': [s[0] for s in same], 'reason': 'same domain only; branches are never auto-merged'}), 'open', onto.now_iso()))
    return None, 'new'

def _make_branch(db, c, clock):
    dom = registrable_domain(c['website']) if c['website'] else None
    venue = onto.ensure_entity(db, 'venue', None, 'brand:' + (dom or onto.branch_key(c['name'])), name=dom or c['name'], origin=c['origin'], clock=clock)
    bid = onto.ensure_entity(db, 'branch', venue, 'branch', name=c['name'], qualifiers={'key': onto.branch_key(c['name']), 'city': c['city']}, origin=c['origin'], clock=clock)
    for alias, lang in ((c['name'], 'en'), (c['name_ko'], 'ko')): onto.add_alias(db, bid, alias, lang)
    if c['website']:
        db.execute('INSERT OR IGNORE INTO external_ids VALUES (?,?,?)', (bid, 'website', _url_key(c['website'])))
        db.execute('INSERT OR IGNORE INTO external_ids VALUES (?,?,?)', (bid, 'domain', dom))
    return bid

# 6. evidence verify --------------------------------------------------------
def evidence_verify(ctx, cursor):
    db, n = ctx.db, 0
    for c in db.execute('SELECT * FROM candidates WHERE verdict IS NULL AND branch_id IS NOT NULL ORDER BY seq LIMIT ?', (ctx.batch * 20,)).fetchall():
        reason = None
        if not c['source_url'] or not re.match(r'https?://', c['source_url']): reason = 'no_source_url'
        elif is_denied(c['source_url']): reason = 'denied_source'
        elif not c['quote'] or not c['quote'].strip(): reason = 'no_quote'
        elif not c['locator'] or c['locator'] in ('{}', 'null'): reason = 'no_locator'
        elif onto.status_for(c['label'], c['source_type']) is None: reason = 'ambiguous_or_unverified_label'
        elif c['value_json'] is None: reason = 'no_value'
        verdict = 'rejected' if reason else 'accepted'
        db.execute('UPDATE candidates SET verdict=?, verdict_reason=? WHERE cand_id=?', (verdict, reason, c['cand_id']))
        if reason == 'ambiguous_or_unverified_label':
            db.execute('INSERT OR IGNORE INTO review_queue VALUES (?,?,?,?,?,?)', (deterministic_id('rq', 'ambig', c['cand_id']), 'ambiguous_extraction', c['cand_id'],
                       json.dumps({'field': c['field'], 'value': json.loads(c['value_json']) if c['value_json'] else None, 'source_url': c['source_url']}, ensure_ascii=False), 'open', onto.now_iso(ctx.clock)))
        n += 1
    return cursor, n

# 7. state update ------------------------------------------------------------
def state_update(ctx, cursor):
    db, n = ctx.db, 0
    items = db.execute('''SELECT DISTINCT w.item_id, w.seq, w.fetched_at FROM work_items w JOIN candidates c USING(item_id)
        WHERE w.seq>? ORDER BY w.seq LIMIT ?''', (int(cursor or 0), ctx.batch)).fetchall()
    for w in items:
        if db.execute("SELECT 1 FROM candidates WHERE item_id=? AND (verdict IS NULL)", (w['item_id'],)).fetchone():
            break  # upstream not finished for this item; resume next cycle
        _apply_work_item(db, w, ctx.clock)
        cursor = w['seq']; n += 1
    reconcile_placeholders(db, ctx.clock)
    onto.refresh_conflicts(db, ctx.clock)
    return str(cursor or 0), n

def _apply_work_item(db, w, clock, extra_slots=()):
    """Apply every accepted candidate of one work item through the temporal state machine.
    extra_slots are (entity_id, attribute, source_url) slots the item used to feed: they get an
    empty snapshot when the new rules no longer produce a value, closing the old facts."""
    groups = {}
    for c in db.execute("SELECT * FROM candidates WHERE item_id=? AND verdict='accepted'", (w['item_id'],)):
        if not c['branch_id']: continue
        for etype, kind, q, attr, value, unit in onto.map_field(c['field'], json.loads(c['value_json']) if c['value_json'] else None):
            if value is None: continue
            eid = onto.subject_entity(db, c['branch_id'], etype, kind, q, clock)
            groups.setdefault((eid, attr, c['source_url']), []).append(dict(value=value, unit=unit or c['unit'],
                status=onto.status_for(c['label'], c['source_type']), source_type=c['source_type'], locator=json.loads(c['locator']),
                quote=c['quote'], origin_ref=c['origin_ref'], collected_at=c['observed_at']))
    for key in extra_slots:
        groups.setdefault(key, [])
    for (eid, attr, url), values in groups.items():
        onto.apply_snapshot(db, eid, attr, url, values, at=w['fetched_at'], clock=clock)

def _item_slots(db, w, clock):
    """Slots (entity, attribute, source_url) this item currently feeds with accepted values."""
    out = set()
    for c in db.execute("SELECT * FROM candidates WHERE item_id=? AND verdict='accepted'", (w['item_id'],)).fetchall():
        if not c['branch_id']: continue
        for etype, kind, q, attr, value, unit in onto.map_field(c['field'], json.loads(c['value_json']) if c['value_json'] else None):
            if value is None: continue
            out.add((onto.subject_entity(db, c['branch_id'], etype, kind, q, clock), attr, c['source_url']))
    return out

def reextract(ctx):
    ctx.db.execute('SAVEPOINT reextract')
    try:
        result = _reextract(ctx)
        ctx.db.execute('RELEASE reextract')
        return result
    except Exception:
        ctx.db.execute('ROLLBACK TO reextract')
        ctx.db.execute('RELEASE reextract')
        raise

def _reextract(ctx):
    """Re-run extraction for every verified_csv work item under the CURRENT rules (run this after
    changing extraction/classification rules; change_detect only processes changed items, so an
    existing DB would otherwise keep the old values). Old candidate rows are re-derived, then the
    state machine closes superseded open facts with correction history — nothing is deleted.
    Idempotent: with unchanged rules a second run only moves last_confirmed_at."""
    if not ctx.csv_path.exists():
        raise FileNotFoundError(f'CSV not found: {ctx.csv_path}')
    db = ctx.db
    csv_rows = _load_csv_rows(ctx)
    items = [w for w in db.execute("SELECT * FROM work_items WHERE origin='verified_csv' ORDER BY seq").fetchall()
             if w['item_id'] in csv_rows]
    at = onto.now_iso(ctx.clock)
    slots = {}
    for w in items:
        slots[w['item_id']] = _item_slots(db, w, ctx.clock)
        db.execute('DELETE FROM candidates WHERE item_id=?', (w['item_id'],))
        if w['item_id'] in csv_rows:
            line, row = csv_rows[w['item_id']]
            _extract_csv_item(db, w, line, row)
    db.execute('UPDATE candidates SET branch_id=(SELECT branch_id FROM candidate_branches b WHERE b.subject_ref=candidates.subject_ref) WHERE branch_id IS NULL')
    while evidence_verify(ctx, None)[1]:
        pass  # verify in batches until every re-derived candidate has a verdict
    for w in items:
        _apply_work_item(db, {**dict(w), 'fetched_at': at}, ctx.clock, extra_slots=slots[w['item_id']])
    reconcile_placeholders(db, ctx.clock)
    onto.refresh_conflicts(db, ctx.clock)
    recompute_views(ctx, None)
    quality_check(ctx, None)
    return {'items': len(items),
            'candidates': db.execute("SELECT count(*) FROM candidates WHERE item_id IN (SELECT item_id FROM work_items WHERE origin='verified_csv')").fetchone()[0]}

def reconcile_placeholders(db, clock=None):
    at = onto.now_iso(clock)
    for (bid,) in db.execute("SELECT entity_id FROM entities WHERE entity_type='branch'").fetchall():
        for etype, kind, attr in onto.REQUIRED_SLOTS:
            known = db.execute('''SELECT f.fact_id FROM facts f JOIN entities e USING(entity_id) WHERE e.parent_id=? AND e.entity_type=? AND e.kind=?
                AND f.attribute=? AND f.effective_to IS NULL AND f.value_json IS NOT NULL LIMIT 1''', (bid, etype, kind, attr)).fetchone()
            canon = onto.ensure_entity(db, etype, bid, kind, clock=clock)
            if known:
                for (ph,) in db.execute('''SELECT f.fact_id FROM facts f JOIN entities e USING(entity_id) WHERE e.parent_id=? AND e.entity_type=? AND e.kind=?
                        AND f.attribute=? AND f.status='unverified' AND f.effective_to IS NULL''', (bid, etype, kind, attr)).fetchall():
                    db.execute("UPDATE facts SET effective_to=?, superseded_by=?, closed_reason='evidence_found' WHERE fact_id=?", (at, known[0], ph))
                    db.execute('INSERT OR IGNORE INTO corrections VALUES (?,?,?,?,?,?,?)', (deterministic_id('corr', ph, known[0]), canon, attr, ph, known[0], 'evidence_found', at))
            else:
                onto.ensure_placeholder(db, canon, attr, at, clock)

# 8. recompute service views --------------------------------------------------
def recompute_views(ctx, cursor):
    from .query import branch_view
    db, at, n = ctx.db, onto.now_iso(ctx.clock), 0
    for (bid,) in db.execute("SELECT entity_id FROM entities WHERE entity_type='branch'").fetchall():
        db.execute('INSERT OR REPLACE INTO service_views VALUES (?,?,?)', (bid, json.dumps(branch_view(db, bid), ensure_ascii=False), at)); n += 1
    return cursor, n

# 9. quality check -----------------------------------------------------------
def quality_check(ctx, cursor):
    db, at, n = ctx.db, onto.now_iso(ctx.clock), 0
    def issue(check, ref, details):
        nonlocal n
        db.execute('INSERT OR IGNORE INTO quality_issues VALUES (?,?,?,?,?,?)', (deterministic_id('qi', check, ref), check, ref, details, 'open', at)); n += 1
    for r in db.execute("SELECT fact_id FROM facts WHERE status='ai_inference' AND effective_to IS NULL"):
        issue('ai_value_present', r[0], 'AI inference must never be served as a value')
    for r in db.execute("SELECT fact_id FROM facts WHERE status IN ('fact','source_claim') AND (quote IS NULL OR source_url IS NULL)"):
        issue('missing_evidence', r[0], 'fact without quote/source')
    for r in db.execute("SELECT f.fact_id, f.value_json FROM facts f WHERE f.attribute='name' AND f.effective_to IS NULL AND f.value_json LIKE '%Visit Seoul%'"):
        issue('portal_title_as_name', r[0], 'directory page title extracted as venue name')
    for r in db.execute("SELECT f.fact_id, f.value_json FROM facts f WHERE f.attribute='open_hours' AND f.effective_to IS NULL AND f.quote LIKE '%maintenance%'"):
        issue('maintenance_window_as_hours', r[0], 'maintenance window extracted as opening hours')
    for r in db.execute("SELECT f.fact_id FROM facts f WHERE f.attribute='temperature_c' AND f.effective_to IS NULL AND f.value_json IS NOT NULL"):
        v = json.loads(db.execute('SELECT value_json FROM facts WHERE fact_id=?', (r[0],)).fetchone()[0])
        vals = v if isinstance(v, list) else [v]
        if any(x < 0 or x > 130 for x in vals): issue('temperature_out_of_range', r[0], str(v))
    for r in db.execute("SELECT fact_id FROM facts WHERE status!='unverified' AND effective_to IS NULL AND last_confirmed_at < datetime('now','-90 days')"):
        issue('stale_90d', r[0], 'not re-confirmed in 90 days')
    for r in db.execute("SELECT e.entity_id, e.name FROM entities e WHERE e.entity_type='branch' AND lower(e.name) LIKE '%crossfit%'"):
        issue('non_sauna_branch_suspect', r[0], f"{r[1]}: typed sauna by active bot; needs sauna evidence")
    return cursor, n

STAGES = [('discover', discover), ('incremental_fetch', incremental_fetch), ('change_detect', change_detect), ('extract', extract),
          ('entity_resolution', entity_resolution), ('evidence_verify', evidence_verify), ('state_update', state_update),
          ('recompute_views', recompute_views), ('quality_check', quality_check)]
