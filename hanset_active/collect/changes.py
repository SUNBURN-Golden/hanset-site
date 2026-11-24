"""Offline official-web observation history and conservative transition detection.

Call writable functions only while holding the collector daemon.lock. No site,
network, model, or twin access belongs here.
"""
import json
import re
from collections import defaultdict
from datetime import datetime

from hanset_active.util.pii import strip_pii
from hanset_active.util.timeutil import KST

from .config import official_site
from .ground import secrets
from .normalize import LEGACY_VERSION, normalize_value, stable_json
from .store import digest, read_claims
from .targets import source_authorization

DETECTOR_VERSION = 'official-web-changes-v1'
# The change feed compares the same hours, price and closure shapes as before.
NORMALIZER_VERSION = 'web-change-v1:' + LEGACY_VERSION
FIELDS = ('hours', 'price_day_pass', 'closures')
SCOPE = ('venue_id', 'venue_key', 'field', 'qualifier_key', 'source_identity', 'target_fingerprint')
HEX = re.compile(r'^[0-9a-f]{64}$')


def has_schema(db):
    return bool(db and db.execute("SELECT 1 FROM sqlite_master WHERE name='web_change_metadata'").fetchone())


def metadata(db):
    return dict(db.execute('SELECT key,value FROM web_change_metadata')) if has_schema(db) else {}


def put_meta(db, key, value):
    db.execute('INSERT INTO web_change_metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (key, str(value)))


def next_seq(db, key):
    table = 'web_change_observations' if key == 'observation_seq' else 'web_change_events'
    column = 'observed_seq' if key == 'observation_seq' else 'event_seq'
    stored = db.execute(f'SELECT coalesce(max({column}),0) FROM {table}').fetchone()[0]
    value = max(stored, int(metadata(db).get(key, '0'))) + 1
    put_meta(db, key, value)
    return value


def safe_text(text):
    if not isinstance(text, str):
        return False
    # Free text is held rather than silently rewritten into different evidence.
    return (strip_pii(text) == text and not any(s in text for s in secrets())
            and not re.search(r'\[(?:phone|email|token|redacted[^\]]*)\]', text, re.I)
            and not re.search(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', text)
            and not re.search(r'디톡스|면역력|살\s*빠|M[Z]\s*사우나|광고|협찬|대회|경력|개인사|'
                              r'competition|championship|personal history|detox|immunity|weight loss|'
                              r'sponsor|partnership|media.?kit', text, re.I))


def closure_state(value):
    if re.search(r'연중무휴|open_every_day|normally open|open (?:every day|daily)|no closures?', value, re.I):
        return 'no_closure'
    if re.search(r'휴무|휴관|폐쇄|closed|closure', value, re.I):
        return 'closure_notice'
    return 'review_only'


def eligible(claim):
    return (bool(HEX.fullmatch(claim.get('claim_id') or ''))
            and not claim.get('superseded') and claim.get('model_confirmed') is not False
            and claim.get('source_kind') == 'web' and claim.get('source_type') == 'own_site'
            and claim.get('status') == 'source_claim' and claim.get('rights') == 'internal_review_only'
            and claim.get('evidence_origin') == 'web_verbatim'
            and claim.get('evidence_role') == 'page_span' and claim.get('verbatim_source') == 1)


def normalized_claim(claim):
    field = claim.get('field')
    if field not in FIELDS:
        return None
    value = claim.get('value')
    normalized = normalize_value(field, value)
    if normalized['reason']:
        return {'field': field, 'reason': normalized['reason']}
    qualifiers = normalized['qualifiers']
    safe_qualifiers = all(safe_text(v) for v in qualifiers.values() if isinstance(v, str))
    grounding = (claim.get('locator') or {}).get('grounding') or {}
    structured = grounding.get('version') == 1 and HEX.fullmatch(grounding.get('span_hash', ''))
    safe = (structured or safe_text(claim.get('quote', ''))) and safe_qualifiers
    if field == 'closures':
        literal = grounding.get('value_literal') is True if structured else isinstance(value, str) and value in claim.get('quote', '')
        safe = safe and safe_text(value) and literal
    if not safe:
        return {'field': field, 'reason': 'unsafe_evidence',
                **({'qualifier_key': stable_json(qualifiers)} if safe_qualifiers else {})}
    return {'field': field, 'qualifier_key': stable_json(qualifiers),
            'value_json': stable_json(normalized['normalized_value']),
            'comparison_key': normalized['comparison_key'], 'reason': None}


def _checked(at):
    parsed = datetime.fromisoformat(at)
    if parsed.tzinfo is None:
        raise ValueError('observation_time_invalid')
    return parsed.astimezone(KST).date().isoformat()


def insert_observation(db, row, claims):
    """Durable daily observation, independently committed after record().

    Only guarded successful page extractions reach this boundary. Repeat
    receipts reuse their original eligible claims but get today's observation.
    """
    auth = row.get('source_authorization')
    if not auth:
        return None
    from .targets import canonical_requested_url
    # Defense in depth for direct callers; loopback is for fixture runs only.
    testing = row.get('_testing', False)
    try:
        final = canonical_requested_url(row.get('final_url', row['url']), testing=testing)
        requested = canonical_requested_url(row['url'], testing=testing)
        if (requested != auth['requested_url'] or requested != auth['source_identity']
                or not official_site(requested) or not official_site(final)
                or not HEX.fullmatch(auth['target_fingerprint'])):
            return None
    except (ValueError, KeyError, TypeError):
        return None
    items = []
    for claim in claims:
        if (not eligible(claim) or claim['venue_id'] != row['venue_id']
                or claim['venue_key'] != row['venue_key']):
            continue
        try:
            if canonical_requested_url(claim['source_url'], testing=testing) != requested:
                continue
        except ValueError:
            continue
        item = normalized_claim(claim)
        if item:
            items.append(item)
    at = row['seen_at']
    day = _checked(at)
    content_hash = row.get('content_hash') or digest(row['text'])
    if not HEX.fullmatch(content_hash):
        raise ValueError('invalid_content_hash')
    identity = (day, row['venue_id'], row['venue_key'], auth['source_identity'], content_hash, auth['target_fingerprint'])
    observation_id = digest(stable_json(identity))
    db.execute('BEGIN IMMEDIATE')
    try:
        if not db.execute('SELECT 1 FROM web_change_observations WHERE observation_id=?', (observation_id,)).fetchone():
            db.execute('''INSERT OR IGNORE INTO web_change_observations
                (observation_id,observed_seq,day,venue_id,venue_key,source_identity,target_fingerprint,
                 requested_url,final_url,content_hash,observed_at,values_json,detector_version,normalizer_version)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (observation_id, next_seq(db, 'observation_seq'), *identity[:3], identity[3], identity[5],
                 requested, final, content_hash, at, stable_json(sorted(items, key=stable_json)),
                 DETECTOR_VERSION, NORMALIZER_VERSION))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return observation_id


def _authorized(targets, row, *, testing=False):
    for target in targets or []:
        if target['venue_id'] == row['venue_id'] and target['venue_key'] == row['venue_key']:
            auth = source_authorization(target, row.get('requested_url', row.get('source_url', row.get('source_identity'))),
                                        row.get('final_url'), testing=testing)
            if auth:
                return auth
    return None


def _groups(items):
    groups = defaultdict(list)
    for item in items:
        if item and not item.get('reason'):
            groups[(item['field'], item['qualifier_key'])].append(item)
    return groups


def _baseline(db, scope, item, *, observation=None, claim=None, last_event=None):
    at = observation['observed_at'] if observation else claim['seen_at']
    values = (*scope, item['value_json'], item['comparison_key'],
              'observation' if observation else 'bootstrap_claim',
              observation['observation_id'] if observation else None,
              claim['claim_id'] if claim else None, at,
              observation['observed_seq'] if observation else 0,
              DETECTOR_VERSION, NORMALIZER_VERSION, 'active', last_event, at)
    db.execute('''INSERT INTO web_change_baselines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(venue_id,venue_key,field,qualifier_key,source_identity,target_fingerprint)
        DO UPDATE SET value_json=excluded.value_json, comparison_key=excluded.comparison_key,
          value_kind=excluded.value_kind,source_observation_id=excluded.source_observation_id,
          source_claim_id=excluded.source_claim_id,observed_at=excluded.observed_at,
          observed_seq=excluded.observed_seq,detector_version=excluded.detector_version,
          normalizer_version=excluded.normalizer_version,state=excluded.state,
          last_event_id=excluded.last_event_id,updated_at=excluded.updated_at''', values)


def bootstrap_baselines(db, targets, *, testing=False):
    """Unanimous authorized historical claims only; no retrospective events."""
    if metadata(db).get('bootstrap_version') or targets is None:
        return
    groups = defaultdict(list)
    for claim in read_claims(db):
        if not eligible(claim):
            continue
        auth = _authorized(targets, claim, testing=testing)
        item = normalized_claim(claim)
        if not auth or not item or item.get('reason'):
            continue
        scope = (claim['venue_id'], claim['venue_key'], item['field'], item['qualifier_key'],
                 auth['source_identity'], auth['target_fingerprint'])
        groups[scope].append((claim, item))
    ambiguous = 0
    for scope, entries in sorted(groups.items()):
        if len({item['comparison_key'] for _, item in entries}) != 1:
            ambiguous += 1
            continue
        claim, item = max(entries, key=lambda pair: (pair[0]['seen_at'], pair[0]['claim_id']))
        # Never replace an actual observation with an historical claim.
        if not db.execute('SELECT 1 FROM web_change_baselines WHERE ' + ' AND '.join(k+'=?' for k in SCOPE), scope).fetchone():
            _baseline(db, scope, item, claim=claim)
    put_meta(db, 'bootstrap_ambiguous', ambiguous)
    put_meta(db, 'bootstrap_version', '1')
    _versions(db)
    db.commit()


def _versions(db):
    for key, value in (('schema_version', '1'), ('detector_version', DETECTOR_VERSION),
                       ('normalizer_version', NORMALIZER_VERSION)):
        put_meta(db, key, value)


def needs_rebaseline(db):
    meta = metadata(db)
    if any(meta.get(k, v) != v for k, v in (('detector_version', DETECTOR_VERSION),
                                           ('normalizer_version', NORMALIZER_VERSION))):
        return True
    if not has_schema(db):
        return False
    return bool(db.execute('''SELECT 1 FROM web_change_baselines WHERE state='active' AND
        (detector_version!=? OR normalizer_version!=?) LIMIT 1''',
        (DETECTOR_VERSION, NORMALIZER_VERSION)).fetchone() or db.execute('''
        SELECT 1 FROM web_change_observations WHERE processed=0 AND
        (detector_version!=? OR normalizer_version!=?) LIMIT 1''',
        (DETECTOR_VERSION, NORMALIZER_VERSION)).fetchone())


def _event(db, observation, kind, field, qualifier, old=None, item=None, reason=None):
    scope = (observation['venue_id'], observation['venue_key'], field, qualifier,
             observation['source_identity'], observation['target_fingerprint'])
    predecessor_observation = old['source_observation_id'] if old else None
    predecessor_claim = old['source_claim_id'] if old else None
    old_key = old['comparison_key'] if old and item else None
    new_key = item['comparison_key'] if item else None
    change_id = digest(stable_json([DETECTOR_VERSION, NORMALIZER_VERSION, scope,
                                   predecessor_observation, predecessor_claim,
                                   observation['observation_id'], kind, old_key, new_key]))
    if db.execute('SELECT 1 FROM web_change_events WHERE change_id=?', (change_id,)).fetchone():
        return change_id
    db.execute('''INSERT INTO web_change_events VALUES
        (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (change_id, next_seq(db, 'event_seq'), kind, *scope,
         observation['requested_url'], observation['final_url'],
         old['value_json'] if old and item else None, item['value_json'] if item else None,
         old_key, new_key, predecessor_observation if item else None,
         observation['observation_id'] if item else None, predecessor_observation, predecessor_claim,
         observation['observed_at'], observation['day'], DETECTOR_VERSION, NORMALIZER_VERSION,
         reason, int(kind in FIELDS and not reason), 'internal_review_only', 0, observation['observed_at']))
    return change_id


def source_conflicts(db):
    """Current comparable scopes disagreeing across independently approved pages."""
    groups = defaultdict(list)
    if has_schema(db):
        for row in db.execute("SELECT * FROM web_change_baselines WHERE state='active'"):
            groups[(row['venue_id'], row['venue_key'], row['field'], row['qualifier_key'])].append(row)
    return {scope for scope, rows in groups.items() if len({r['comparison_key'] for r in rows}) > 1}


def review_holds(db):
    """Latest authorized observations can hold an otherwise clean past event."""
    active = [dict(r) for r in db.execute("SELECT * FROM web_change_baselines WHERE state='active'")]
    authorizations = {(r['venue_id'], r['venue_key'], r['target_fingerprint']) for r in active}
    latest = {}
    for raw in db.execute('SELECT * FROM web_change_observations WHERE processed=1 ORDER BY observed_seq'):
        row = dict(raw)
        if (row['venue_id'], row['venue_key'], row['target_fingerprint']) in authorizations:
            latest[(row['venue_id'], row['venue_key'], row['source_identity'])] = row
    holds = {}
    for row in latest.values():
        items = json.loads(row['values_json'])
        for (field, qualifier), entries in _groups(items).items():
            if len({e['comparison_key'] for e in entries}) > 1:
                holds[(row['venue_id'], row['venue_key'], field, qualifier)] = 'within_observation_conflict'
        for item in items:
            if item.get('reason'):
                qualifiers = {item['qualifier_key']} if 'qualifier_key' in item else {
                    r['qualifier_key'] for r in active if r['venue_id'] == row['venue_id']
                    and r['venue_key'] == row['venue_key'] and r['field'] == item['field']}
                for qualifier in qualifiers:
                    holds[(row['venue_id'], row['venue_key'], item['field'], qualifier)] = item['reason']
    return holds


def _derive(db, observation):
    items = json.loads(observation['values_json'])
    groups = _groups(items)
    diagnostics = {i['reason'] for i in items if i.get('reason')}
    old_rows = [dict(r) for r in db.execute('''SELECT * FROM web_change_baselines WHERE
        venue_id=? AND venue_key=? AND source_identity=? AND target_fingerprint=? AND state='active' ''',
        tuple(observation[k] for k in ('venue_id', 'venue_key', 'source_identity', 'target_fingerprint')))]
    old_map = {(r['field'], r['qualifier_key']): r for r in old_rows}
    # Held evidence gets an immutable diagnostic with no unsafe values. Keep
    # the comparable baseline and its last clean value event unchanged.
    for item in items:
        if item.get('reason') and 'qualifier_key' in item:
            old = old_map.get((item['field'], item['qualifier_key']))
            if old:
                _event(db, observation, item['field'], item['field'], item['qualifier_key'],
                       old=old, reason=item['reason'])
    present_fields = {field for field, _ in groups}
    invalid_fields = {i['field'] for i in items if i.get('reason')}
    for (field, qualifier), entries in sorted(groups.items()):
        if len({entry['comparison_key'] for entry in entries}) != 1:
            diagnostics.add('within_observation_conflict')
            invalid_fields.add(field)
            old = old_map.get((field, qualifier))
            if old:
                _event(db, observation, field, field, qualifier, old=old, reason='within_observation_conflict')
            continue
        item = entries[0]
        scope = (observation['venue_id'], observation['venue_key'], field, qualifier,
                 observation['source_identity'], observation['target_fingerprint'])
        old = old_map.get((field, qualifier))
        if old and old['observed_seq'] >= observation['observed_seq']:
            continue
        last_event = old['last_event_id'] if old else None
        if not old:
            if any(r['field'] == field for r in old_rows):
                _event(db, observation, 'qualifier_added', field, qualifier, reason='qualifier_added')
        elif old['comparison_key'] != item['comparison_key']:
            reason = None
            if field == 'closures':
                state = closure_state(json.loads(item['value_json']))
                reason = None if state == 'closure_notice' else ('no_closure' if state == 'no_closure' else 'unsupported_value')
            others = db.execute('''SELECT comparison_key FROM web_change_baselines WHERE venue_id=?
                AND venue_key=? AND field=? AND qualifier_key=? AND source_identity!=? AND state='active' ''', scope[:5]).fetchall()
            if any(r[0] != item['comparison_key'] for r in others):
                reason = 'source_conflict'
            last_event = _event(db, observation, field, field, qualifier, old, item, reason)
        _baseline(db, scope, item, observation=observation, last_event=last_event)
    for (field, qualifier), old in sorted(old_map.items()):
        # Complete absence of a field never implies a closure removal or change.
        if field in present_fields and field not in invalid_fields and (field, qualifier) not in groups:
            _event(db, observation, 'qualifier_missing', field, qualifier, old, reason='qualifier_missing')
    diagnostic = sorted(diagnostics)[0] if diagnostics else None
    db.execute('UPDATE web_change_observations SET processed=1,diagnostic=? WHERE observation_id=?',
               (diagnostic, observation['observation_id']))


def process_pending(db, targets, *, testing=False):
    if needs_rebaseline(db):
        put_meta(db, 'diagnostic', 'rebaseline_required')
        db.commit()
        return {'reason': 'rebaseline_required', 'processed': 0}
    if targets is None:
        return {'reason': 'targets_invalid', 'processed': 0}
    _versions(db)
    retired = 0
    for raw in db.execute("SELECT * FROM web_change_baselines WHERE state='active'").fetchall():
        baseline = dict(raw)
        auth = _authorized(targets, baseline, testing=testing)
        if not auth or auth['target_fingerprint'] != baseline['target_fingerprint']:
            db.execute("UPDATE web_change_baselines SET state='needs_rebaseline' WHERE " +
                       ' AND '.join(k+'=?' for k in SCOPE), tuple(baseline[k] for k in SCOPE))
            retired += 1
    put_meta(db, 'authorization_retired_baselines', retired)
    db.commit()
    processed, failures = 0, 0
    blocked = set()
    for raw in db.execute('SELECT * FROM web_change_observations WHERE processed=0 ORDER BY observed_seq').fetchall():
        observation = dict(raw)
        source_scope = tuple(observation[k] for k in ('venue_id', 'venue_key', 'source_identity'))
        if source_scope in blocked:
            continue
        db.execute('BEGIN IMMEDIATE')
        db.execute('SAVEPOINT detect')
        try:
            auth = _authorized(targets, observation, testing=testing)
            if not auth or auth['target_fingerprint'] != observation['target_fingerprint']:
                _event(db, observation, 'authorization_changed', '', '{}', reason='authorization_changed')
                db.execute("UPDATE web_change_observations SET processed=2,diagnostic='authorization_changed' WHERE observation_id=?",
                           (observation['observation_id'],))
            else:
                previous_authorizations = db.execute('''SELECT 1 FROM web_change_baselines WHERE
                    venue_id=? AND venue_key=? AND target_fingerprint!=? LIMIT 1''',
                    (observation['venue_id'], observation['venue_key'], observation['target_fingerprint'])).fetchone()
                current_authorization = db.execute('''SELECT 1 FROM web_change_baselines WHERE
                    venue_id=? AND venue_key=? AND target_fingerprint=? AND state='active' LIMIT 1''',
                    (observation['venue_id'], observation['venue_key'], observation['target_fingerprint'])).fetchone()
                if previous_authorizations and not current_authorization:
                    _event(db, observation, 'authorization_changed', '', '{}', reason='authorization_changed')
                    db.execute('''UPDATE web_change_baselines SET state='needs_rebaseline' WHERE
                        venue_id=? AND venue_key=? AND target_fingerprint!=?''',
                        (observation['venue_id'], observation['venue_key'], observation['target_fingerprint']))
                _derive(db, observation)
            db.execute('RELEASE detect')
            processed += 1
        except Exception:
            db.execute('ROLLBACK TO detect')
            db.execute('RELEASE detect')
            db.execute("UPDATE web_change_observations SET diagnostic='detector_error' WHERE observation_id=?",
                       (observation['observation_id'],))
            blocked.add(source_scope)
            failures += 1
        db.commit()
    return {'reason': 'detector_error' if failures else 'ok', 'processed': processed, 'failures': failures}


def rebaseline_candidates(db, targets, *, testing=False):
    """Read-only reseed plan, shared by dry-run and explicit rebaseline."""
    candidates = {}
    if targets is None or not has_schema(db):
        return candidates
    for claim in read_claims(db):
        if not eligible(claim):
            continue
        auth, item = _authorized(targets, claim, testing=testing), normalized_claim(claim)
        if auth and item and not item.get('reason'):
            scope = (claim['venue_id'], claim['venue_key'], item['field'], item['qualifier_key'],
                     auth['source_identity'], auth['target_fingerprint'])
            rank = (0, claim['seen_at'], claim['claim_id'])
            if scope not in candidates or rank > candidates[scope][0]:
                candidates[scope] = (rank, item, None, claim)
    for raw in db.execute('SELECT * FROM web_change_observations ORDER BY observed_seq'):
        observation = dict(raw)
        auth = _authorized(targets, observation, testing=testing)
        if not auth or auth['target_fingerprint'] != observation['target_fingerprint']:
            continue
        # Re-apply current rules to the typed snapshot; never fetch raw pages.
        items = []
        for snapshot in json.loads(observation['values_json']):
            if snapshot.get('reason'):
                continue
            field = snapshot['field']
            value = json.loads(snapshot['value_json'])
            if field in ('hours', 'price_day_pass'):
                value = {**json.loads(snapshot['qualifier_key']),
                         ('hours' if field == 'hours' else 'amount'): value}
            item = normalized_claim({'field': field, 'value': value,
                                     'quote': value if isinstance(value, str) else stable_json(value)})
            if item:
                items.append(item)
        for (field, qualifier), entries in _groups(items).items():
            if len({i['comparison_key'] for i in entries}) != 1:
                continue
            scope = (observation['venue_id'], observation['venue_key'], field, qualifier,
                     auth['source_identity'], auth['target_fingerprint'])
            candidates[scope] = ((1, observation['observed_seq']), entries[0], observation, None)
    return candidates


def rebaseline(db, targets, *, testing=False):
    """Explicit offline reseed with today's rules, without generating transitions."""
    if targets is None:
        return {'reason': 'targets_invalid', 'reseeded': 0}
    db.execute('BEGIN IMMEDIATE')
    try:
        candidates = rebaseline_candidates(db, targets, testing=testing)
        db.execute("UPDATE web_change_baselines SET state='needs_rebaseline'")
        for scope, (_, item, observation, claim) in sorted(candidates.items()):
            _baseline(db, scope, item, observation=observation, claim=claim)
        db.execute("UPDATE web_change_observations SET processed=2,diagnostic='rebaselined' WHERE processed=0")
        _versions(db)
        put_meta(db, 'bootstrap_version', '1')
        put_meta(db, 'rebaseline_generation', int(metadata(db).get('rebaseline_generation', '0')) + 1)
        put_meta(db, 'diagnostic', '')
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {'reason': 'ok', 'reseeded': len(candidates)}


def change_report(db):
    result = {'pending_observations': 0, 'observations': 0, 'baselines': 0, 'events': 0,
              'diagnostics': {}, 'event_counts': {}, 'rebaseline_required': False, 'metadata': {}}
    if not has_schema(db):
        return result
    result['metadata'] = metadata(db)
    for table, key in (('web_change_observations', 'observations'), ('web_change_baselines', 'baselines'),
                       ('web_change_events', 'events')):
        result[key] = db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
    result['pending_observations'] = db.execute('SELECT count(*) FROM web_change_observations WHERE processed=0').fetchone()[0]
    result['diagnostics'] = dict(db.execute('SELECT diagnostic,count(*) FROM web_change_observations WHERE diagnostic IS NOT NULL GROUP BY diagnostic'))
    result['rebaseline_required'] = needs_rebaseline(db)
    result['event_counts'] = dict(db.execute('SELECT event_kind,count(*) FROM web_change_events GROUP BY event_kind'))
    return result
