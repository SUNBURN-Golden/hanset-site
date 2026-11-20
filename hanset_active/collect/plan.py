"""Read-only gap projection and frozen daily ordering. No publication or IG access."""
import hashlib
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Protocol

from hanset_active.gaps import KEY_FIELDS, NEED_REASONS, project_needs
from hanset_active.util.timeutil import Clock, kst_now
from hanset_active.util.urls import is_excluded
from hanset_active.venues import is_article_title
from hanset_twin.daemon import AlreadyRunning, ProcessLock

from . import CollectError
from .config import (IG_READ_MAX, MODEL_CALL_CAP, MODEL_INPUT_CAP, MODEL_OUTPUT_CAP,
                     configured_model, handle_valid, outside)
from .deepseek import DeepSeek, INPUT_LIMIT, OUTPUT_TOKENS, credentials_present
from .ground import redact
from .ingest import load_watchlist
from .state import State, atomic_json
from .targets import derive_seed_targets, derive_target, merge_targets, ownership_index, parse_targets

POLICY = 'gap-priority-v1'
MAX_PLAN_BYTES = 512_000
INSTRUCTION = ('Rank only the supplied anonymous venue indices for evidence review. '
               'Prefer useful combinations of unresolved fields within each gap_count group. '
               'Invent nothing. Return only JSON with exactly one key: '
               '{"order":[0,1]}. Include every supplied index exactly once, no other values.')
DIGEST_RE = re.compile(r'[a-f0-9]{64}')
DIAGNOSTIC_CODES = frozenset({
    'active_db_missing', 'active_db_unreadable', 'watchlist_missing', 'watchlist_invalid',
    'watchlist_excluded', 'stale_gap_state', 'targets_missing', 'targets_empty', 'targets_invalid',
    'plan_invalid', 'plan_symlink', 'targets_changed', 'removed_targets', 'unplanned_targets',
    'adhoc_targets_local_order', 'ig_window_passed', 'ig_halted_today', 'incomplete_discarded',
    'manual_identity_unmatched', 'manual_identity_ambiguous', 'derived_http_only',
    'derived_domain_denied', 'derived_suppressed', 'derived_truncated',
})
RANK_REASONS = frozenset({
    'local', 'testing_local', 'model_ranked', 'decision_cached', 'fallback_no_candidates',
    'fallback_missing_credentials', 'fallback_budget_reserved', 'fallback_model_invalid',
    'fallback_model_timeout', 'fallback_model_transport', 'fallback_model_rate_limited',
    'fallback_model_unavailable', 'fallback_model_auth', 'fallback_model_budget_exhausted',
    'fallback_model_insufficient_balance',
})


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def identity(row):
    return row['venue_id'], row['venue_key']


def target_id(target):
    return digest({'identity': identity(target), 'urls': target['urls'],
                   'domains': sorted(target['domains']), 'tavily': target['tavily']})


def target_set_digest(targets):
    # File order matters for identical instances and stable append ordering.
    return digest([target_id(target) for target in targets])


def load_targets(path, *, testing=False, disabled=None):
    if not path.exists() and not path.is_symlink():
        return [], 'missing'
    try:
        targets = parse_targets(path, testing=testing, disabled=disabled)
        return targets, 'ok' if targets else 'empty'
    except CollectError:
        return [], 'invalid'


def _local_key(row):
    count = row['gap_count']
    return (row['state'] == 'closed', count is None, -(count or 0),
            -row['importance'], *identity(row))


def _watchlist(settings, notes):
    path = settings.data_dir / 'watchlist.json'
    if not path.exists() and not path.is_symlink():
        notes['watchlist_missing'] += 1
        return []
    try:
        entries = load_watchlist(path)
    except CollectError:
        notes['watchlist_invalid'] += 1
        return []
    result = []
    for entry in entries:
        if is_excluded(entry['name'], entry['venue_key']):
            notes['watchlist_excluded'] += 1
        elif (_ident(entry) and _text(entry['name']) and _text(entry['handle'])):
            result.append(entry)
        else:
            notes['watchlist_invalid'] += 1
    return result


def _snapshot_rows(db, targets, watchlist, *, now, ownership=()):
    notes, candidates, fingerprints = Counter(), {}, []
    for channel, entries in (('web', targets), ('ig', watchlist)):
        for entry in entries:
            key = identity(entry)
            row = candidates.setdefault(key, {
                'venue_id': key[0], 'venue_key': key[1], 'gap_count': None,
                'fields': [], 'importance': 0, 'channels': [], 'state': 'unknown',
                'identity_status': 'unknown', 'reason': 'identity_unknown',
            })
            if channel not in row['channels']:
                row['channels'].append(channel)
    if db is not None:
        for key, row in sorted(candidates.items()):
            venue = db.execute('SELECT * FROM venues WHERE venue_id=? AND venue_key=?', key).fetchone()
            if venue is None:
                continue
            projected = project_needs(db, key[0], now=now)
            row.update({k: v for k, v in projected.items() if k != 'diagnostics'})
            row.update(state=venue['state'] or 'unknown', identity_status='resolved')
            notes.update(projected['diagnostics'])
            # Hash metadata only; never persist quotes or source pages.
            evidence = [dict(r) for r in db.execute(
                'SELECT obs_id,field,value_hash,label_status,source_url,source_type,observed_at '
                'FROM observations WHERE venue_key=? ORDER BY obs_id', (key[1],))]
            current = [dict(r) for r in db.execute(
                'SELECT vf_id,field,value_hash,label_status,obs_id,valid_from,valid_to '
                'FROM venue_fields WHERE venue_id=? ORDER BY vf_id', (key[0],))]
            gaps = [dict(r) for r in db.execute(
                'SELECT field,state,obs_id,next_at,updated_at FROM gaps WHERE venue_id=? ORDER BY field', (key[0],))]
            fingerprints.append(digest([key, venue['type'], venue['state'], venue['website'],
                                        venue['domains'], evidence, current, gaps]))
    for row in candidates.values():
        row['channels'].sort()
    ranked = sorted(candidates.values(), key=_local_key)
    return ranked, digest([ranked, fingerprints, ownership]), notes


def _readonly(settings):
    path = settings.active_dir / 'active.sqlite3'
    if not path.is_file():
        raise FileNotFoundError
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        return db
    except BaseException:
        db.close()
        raise


def snapshot(settings, targets, watchlist, *, now):
    """Compatibility entry point: materialize one read-only transaction."""
    db = None
    try:
        db = _readonly(settings)
        return _snapshot_rows(db, targets, watchlist, now=now)
    except (OSError, sqlite3.Error, ValueError) as error:
        rows, fingerprint, notes = _snapshot_rows(None, targets, watchlist, now=now)
        notes['active_db_missing' if isinstance(error, FileNotFoundError) else 'active_db_unreadable'] += 1
        return rows, fingerprint, notes
    finally:
        if db is not None:
            db.close()


def assemble_targets(settings, *, now, testing=False, include_watchlist=True):
    """One materialized authorization/evidence snapshot, closed before ranking/I/O.

    Planning may fall back to manual input on a DB failure. Change detection
    must inspect db_status and defer instead of retiring derived authorization.
    """
    notes, disabled = Counter(), []
    manual, status = load_targets(settings.data_dir / 'web_targets.json',
                                  testing=testing, disabled=disabled)
    watchlist = _watchlist(settings, notes) if include_watchlist else []
    targets, db_status, db = manual, 'ok', None
    try:
        db = _readonly(settings)
        venues = [dict(r) for r in db.execute('SELECT * FROM venues ORDER BY venue_id')]
        aliases = [dict(r) for r in db.execute('SELECT * FROM venue_aliases ORDER BY alias_id')]
        marker = db.execute("SELECT cursor FROM run_checkpoints WHERE checkpoint_id='seed_freeze'").fetchone()
        seed_id = marker['cursor'] if marker else None
        seeds = [dict(r) for r in db.execute(
            'SELECT * FROM seed_venues WHERE seed_id=? ORDER BY seed_venue_id', (seed_id,))]
        seed_domains = [dict(r) for r in db.execute(
            'SELECT * FROM seed_domains WHERE seed_id=? ORDER BY seed_domain_id', (seed_id,))]
        ownership = ownership_index(venues, aliases)
        derived = []
        if status != 'invalid':
            seed_targets = derive_seed_targets(seeds, seed_domains, ownership, notes)
            seeds_by_identity = defaultdict(list)
            for target in seed_targets:
                seeds_by_identity[identity(target)].append(target)
            for venue in venues:
                if (venue['state'] == 'closed' or is_article_title(venue['name'])
                        or is_excluded(venue['name'], venue['venue_key'])):
                    continue
                gaps = {r['field'] for r in db.execute(
                    "SELECT field FROM gaps WHERE venue_id=? AND state IN ('open','conflict')",
                    (venue['venue_id'],))} & set(KEY_FIELDS)
                if not gaps:
                    continue
                projected = project_needs(db, venue['venue_id'], now=now)
                # Missing unrelated fields must not revive stale-satisfied gaps.
                if not gaps.intersection(f['field_id'] for f in projected['fields']):
                    continue
                target = derive_target(venue, ownership[2][venue['venue_id']], notes,
                                       seeds=seeds_by_identity.get(identity(venue), ()))
                if target:
                    derived.append((target, {**venue, **projected}))
            derived.sort(key=lambda pair: _local_key(pair[1]))
            targets = merge_targets(manual, [t for t, _ in derived], disabled,
                                    ownership, notes, seeds=seed_targets, testing=testing)
        materialized = _snapshot_rows(db, targets, watchlist, now=now,
                                      ownership=[venues, aliases, seed_id, seeds, seed_domains])
    except (OSError, sqlite3.Error, ValueError, TypeError):
        # Discard ALL partially derived/resolved authorization on failure.
        db_status = 'missing' if not (settings.active_dir / 'active.sqlite3').is_file() else 'unreadable'
        notes = Counter({k: v for k, v in notes.items() if k.startswith('watchlist_')})
        notes['active_db_' + db_status] += 1
        targets = manual
        materialized = _snapshot_rows(None, targets, watchlist, now=now)
    finally:
        if db is not None:
            db.close()
    rows, fingerprint, snapshot_notes = materialized
    notes.update(snapshot_notes)
    return {'targets': targets, 'targets_status': status, 'db_status': db_status,
            'watchlist': watchlist, 'snapshot': (rows, fingerprint, notes)}


class Ranker(Protocol):
    mode: str
    model: str | None

    def rank(self, candidates):
        """Return (complete permutation of request indices, fixed reason)."""


class LocalRanker:
    mode = 'local'
    model = None

    def __init__(self, reason='local'):
        if reason not in RANK_REASONS:
            raise ValueError('Invalid ranking reason')
        self.reason = reason

    def rank(self, candidates):
        return list(range(len(candidates))), self.reason


def valid_order(answer, count):
    return (isinstance(answer, dict) and set(answer) == {'order'}
            and isinstance(answer['order'], list) and len(answer['order']) == count
            and all(type(i) is int for i in answer['order'])
            and sorted(answer['order']) == list(range(count)))


def model_payload(candidates):
    groups = []
    for index, row in enumerate(candidates):
        if not groups or groups[-1]['gap_count'] != row['gap_count']:
            groups.append({'gap_count': row['gap_count'], 'venues': []})
        groups[-1]['venues'].append({'index': index,
                                     'fields': [f['field_id'] for f in row['fields']],
                                     'channels': row['channels']})
    return {'groups': groups}


class DeepSeekRanker:
    mode = 'model'

    def __init__(self, state, *, model, session=None):
        self.state, self.model, self.session = state, configured_model(model), session

    def rank(self, candidates):
        local = list(range(len(candidates)))
        if not candidates:
            return local, 'fallback_no_candidates'
        if not credentials_present():
            return local, 'fallback_missing_credentials'
        payload = model_payload(candidates)
        chars = len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))
        budgets = self.state.data['budgets']
        # Headroom: two ranking attempts (primary + one Qwen backup) plus one
        # full extraction request. reserve_model still enforces caps per attempt.
        if (chars > INPUT_LIMIT or not self.state.can_model(chars, OUTPUT_TOKENS)
                or budgets['model_calls'] + 3 > MODEL_CALL_CAP
                or budgets['input_chars'] + 2 * chars + INPUT_LIMIT > MODEL_INPUT_CAP
                or budgets['output_tokens'] + 3 * OUTPUT_TOKENS > MODEL_OUTPUT_CAP):
            return local, 'fallback_budget_reserved'
        brain = DeepSeek(self.state, model=self.model, session=self.session)
        try:
            answer = brain.ask(payload, instruction=INSTRUCTION)
            if not valid_order(answer, len(candidates)):
                return local, 'fallback_model_invalid'
            return answer['order'], 'model_ranked'
        except CollectError as error:
            reason = 'fallback_' + error.reason
            return local, reason if reason in RANK_REASONS else 'fallback_model_invalid'
        finally:
            brain.close()


def choose_ranker(settings, state, *, planner=None, planner_session=None, testing=False, ranking='model'):
    if planner is not None:
        if type(planner) is DeepSeekRanker:
            # An injected ranker must charge this run's authoritative State,
            # rather than an independently loaded checkpoint object.
            return DeepSeekRanker(state, model=planner.model,
                                  session=planner_session if planner_session is not None else planner.session)
        return planner
    if testing or ranking == 'local':
        return LocalRanker('testing_local' if testing else 'local')
    return DeepSeekRanker(state, model=settings.model, session=planner_session)


def _safe_path(path):
    # atomic_json resolves paths: reject links first, including directory links.
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise CollectError('plan_symlink')
    outside(path)


def _write(path, value):
    _safe_path(path)
    if len((json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode()) > MAX_PLAN_BYTES:
        raise CollectError('plan_invalid')
    atomic_json(path, value)


def _read(path):
    _safe_path(path)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as handle:
        if os.fstat(handle.fileno()).st_size > MAX_PLAN_BYTES:
            raise ValueError('Oversized plan')
        blob = handle.read(MAX_PLAN_BYTES + 1)
        if len(blob) > MAX_PLAN_BYTES:
            raise ValueError('Oversized plan')
    try:
        return json.loads(blob.decode('utf-8'))
    except RecursionError:
        raise ValueError('Invalid plan nesting') from None


def _text(value, limit=200):
    return isinstance(value, str) and 0 < len(value) <= limit and redact(value) == value


def _hash(value):
    return isinstance(value, str) and DIGEST_RE.fullmatch(value) is not None


def _keys(value, keys):
    return isinstance(value, dict) and set(value) == set(keys.split())


def _ident(value):
    from .ground import secrets
    vid = value.get('venue_id')
    safe_id = (_text(vid) or (_hash(vid) and not any(secret in vid for secret in secrets())))
    return safe_id and _text(value.get('venue_key'))


def _field_list(fields):
    if not isinstance(fields, list) or len(fields) > len(KEY_FIELDS):
        return False
    seen = set()
    from hanset_active.config import load_profile
    configs = {f['id']: f for f in load_profile()['fields'] if f['id'] in KEY_FIELDS}
    for field in fields:
        if (not _keys(field, 'field_id importance reason') or field['field_id'] not in configs
                or field['field_id'] in seen or field['reason'] not in NEED_REASONS
                or field['reason'] in ('identity_unknown', 'venue_type_unknown', 'venue_closed')
                or type(field['importance']) not in (int, float)
                or field['importance'] != configs[field['field_id']]['importance']):
            return False
        seen.add(field['field_id'])
    return fields == sorted(fields, key=lambda f: (-f['importance'], f['field_id']))


def window(day):
    return {'date': day, 'start': day + 'T13:00:00+09:00', 'end': day + 'T14:00:00+09:00'}


def validate_plan(plan, day):
    """Strict bounded artifact schema; loaded URLs are never executed."""
    try:
        if not _keys(plan, 'schema_version plan_id input_digest policy_version target_set_digest targets_status day timezone generated_at ranking ranked_venues web_targets ig_window ig_read_list diagnostics auto_publish_to_site'):
            return False
        if (type(plan['schema_version']) is not int or plan['schema_version'] != 1
                or plan['policy_version'] != POLICY or plan['timezone'] != 'Asia/Seoul'
                or date.fromisoformat(plan['day']).isoformat() != day or plan['day'] != day
                or plan['ig_window'] != window(day) or plan['auto_publish_to_site'] is not False
                or plan['targets_status'] not in ('ok', 'missing', 'empty', 'invalid')
                or not all(_hash(plan[k]) for k in ('plan_id', 'input_digest', 'target_set_digest'))):
            return False
        stamp = datetime.fromisoformat(plan['generated_at'])
        if stamp.utcoffset() != timedelta(hours=9):
            return False
        rank = plan['ranking']
        from .config import ALLOWED_MODELS
        if (not _keys(rank, 'mode model reason decision_digest candidate_count')
                or rank['mode'] not in ('model', 'local') or rank['reason'] not in RANK_REASONS
                or (rank['model'] is not None and rank['model'] not in ALLOWED_MODELS)
                or not _hash(rank['decision_digest']) or type(rank['candidate_count']) is not int
                or not 0 <= rank['candidate_count'] <= 100):
            return False
        model_decision = rank['reason'] in ('model_ranked', 'decision_cached')
        if (rank['mode'] == 'model') != model_decision or (model_decision and rank['model'] is None):
            return False
        rows = plan['ranked_venues']
        if not isinstance(rows, list) or len(rows) > 1100:
            return False
        identities = {}
        priority_keys = []
        for row in rows:
            if (not _keys(row, 'venue_id venue_key gap_count fields importance channels state identity_status reason')
                    or not _ident(row) or identity(row) in identities or not _field_list(row['fields'])
                    or row['state'] not in ('active', 'opening_soon', 'closed', 'unknown')
                    or row['identity_status'] not in ('resolved', 'unknown')
                    or (row['reason'] is not None and row['reason'] not in NEED_REASONS)
                    or not isinstance(row['channels'], list) or not row['channels']
                    or row['channels'] != sorted(set(row['channels']))
                    or not set(row['channels']) <= {'web', 'ig'}
                    or type(row['importance']) not in (int, float)
                    or row['importance'] != round(sum(f['importance'] for f in row['fields']), 6)):
                return False
            if row['gap_count'] is None:
                if row['fields'] or row['reason'] not in ('identity_unknown', 'venue_type_unknown'):
                    return False
            elif (type(row['gap_count']) is not int or row['gap_count'] != len(row['fields'])
                  or row['identity_status'] != 'resolved'
                  or row['reason'] != ('venue_closed' if row['state'] == 'closed' else None)):
                return False
            if row['identity_status'] == 'unknown' and row['reason'] != 'identity_unknown':
                return False
            identities[identity(row)] = row
            priority_keys.append((row['state'] == 'closed', row['gap_count'] is None,
                                  -(row['gap_count'] or 0)))
        if priority_keys != sorted(priority_keys):
            return False
        if rank['candidate_count'] != min(100, sum(bool(r['gap_count']) and r['state'] != 'closed' for r in rows)):
            return False
        venue_positions = {key: index for index, key in enumerate(identities)}
        targets = plan['web_targets']
        if not isinstance(targets, list) or len(targets) > 100:
            return False
        ids = set()
        target_positions = []
        for target in targets:
            if (not _keys(target, 'target_id venue_id venue_key urls occurrences')
                    or not _ident(target) or not _hash(target['target_id']) or target['target_id'] in ids
                    or identity(target) not in identities or 'web' not in identities[identity(target)]['channels']
                    or type(target['occurrences']) is not int or not 1 <= target['occurrences'] <= 100
                    or not isinstance(target['urls'], list) or len(target['urls']) > 3
                    or not all(_text(url, 500) for url in target['urls'])):
                return False
            ids.add(target['target_id'])
            target_positions.append(venue_positions[identity(target)])
        if target_positions != sorted(target_positions):
            return False
        reads = plan['ig_read_list']
        if not isinstance(reads, list) or len(reads) > IG_READ_MAX:
            return False
        seen, handles = set(), set()
        read_positions = []
        for index, read in enumerate(reads, 1):
            if (not _keys(read, 'rank venue_id venue_key handle name gap_count fields') or not _ident(read)
                    or type(read['rank']) is not int or read['rank'] != index
                    or not _text(read['name']) or not handle_valid(read['handle'])
                    or is_excluded(read['name'], read['venue_key'])
                    or identity(read) in seen or read['handle'].lower() in handles
                    or identity(read) not in identities):
                return False
            venue = identities[identity(read)]
            if (venue['state'] == 'closed' or venue['identity_status'] != 'resolved'
                    or 'ig' not in venue['channels'] or not venue['gap_count']
                    or type(read['gap_count']) is not int or read['gap_count'] != venue['gap_count']
                    or read['fields'] != [f['field_id'] for f in venue['fields']]):
                return False
            seen.add(identity(read))
            handles.add(read['handle'].lower())
            read_positions.append(venue_positions[identity(read)])
        if read_positions != sorted(read_positions):
            return False
        if not isinstance(plan['diagnostics'], list) or len(plan['diagnostics']) > len(DIAGNOSTIC_CODES):
            return False
        seen_codes = set()
        for note in plan['diagnostics']:
            if (not _keys(note, 'code count') or note['code'] not in DIAGNOSTIC_CODES
                    or note['code'] in seen_codes or type(note['count']) is not int or not 1 <= note['count'] <= 10000):
                return False
            seen_codes.add(note['code'])
        return plan['plan_id'] == digest({k: v for k, v in plan.items() if k != 'plan_id'})
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _diagnostics(notes):
    return [{'code': code, 'count': min(count, 10000)} for code, count in sorted(notes.items())
            if code in DIAGNOSTIC_CODES and count > 0]


def _apply_rank(rows, candidates, order):
    positions = {identity(candidates[i]): pos for pos, i in enumerate(order)}
    return sorted(rows, key=lambda row: (
        row['state'] == 'closed', row['gap_count'] is None, -(row['gap_count'] or 0),
        positions.get(identity(row), len(candidates)), -row['importance'], *identity(row)))


def _decision(path, candidates, ranker):
    try:
        saved = _read(path)
        if (not _keys(saved, 'policy_version model order decision_digest')
                or saved['policy_version'] != POLICY or saved['model'] != ranker.model
                or saved['decision_digest'] != digest({k: v for k, v in saved.items() if k != 'decision_digest'})
                or not isinstance(saved['order'], list)
                or any(not _keys(item, 'venue_id venue_key') for item in saved['order'])):
            return None
        lookup = {identity(row): index for index, row in enumerate(candidates)}
        order = [lookup[identity(row)] for row in saved['order']]
        return order if valid_order({'order': order}, len(candidates)) else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def ensure_daily_plan(settings, state, targets, targets_status, *, clock, day=None,
                      planner=None, planner_session=None, testing=False, ranking='model', assembled=None):
    now = kst_now(clock)
    day = day or now.date().isoformat()
    if date.fromisoformat(day).isoformat() != day:
        raise CollectError('plan_invalid')
    directory = settings.data_dir / 'plans'
    path = directory / (day + '.json')
    notes = Counter()
    target_digest = target_set_digest(targets)
    try:
        saved = _read(path)
        if validate_plan(saved, day):
            # Frozen plans keep their original target digest; replay reports drift.
            path.parent.chmod(0o700)
            path.chmod(0o600)
            return saved
        notes['plan_invalid'] += 1
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError):
        notes['plan_invalid'] += 1
    if targets_status != 'ok':
        notes['targets_' + targets_status] += 1
    watchlist = assembled['watchlist'] if assembled is not None else _watchlist(settings, notes)
    rows, snapshot_digest, snapshot_notes = (assembled['snapshot'] if assembled is not None
                                             else snapshot(settings, targets, watchlist, now=now))
    notes.update(snapshot_notes)
    ranker = choose_ranker(settings, state, planner=planner, planner_session=planner_session,
                           testing=testing, ranking=ranking)
    base = digest({'policy_version': POLICY, 'day': day, 'snapshot': snapshot_digest,
                   'watchlist': watchlist, 'targets': target_digest, 'targets_status': targets_status,
                   'fields': KEY_FIELDS})
    input_digest = digest([base, ranker.mode, ranker.model])
    marker = directory / ('.incomplete-' + day + '.json')
    marker_body = {'input_digest': input_digest, 'target_set_digest': target_digest}
    try:
        if _read(marker) != marker_body:
            notes['incomplete_discarded'] += 1
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError):
        notes['incomplete_discarded'] += 1
    _write(marker, marker_body)
    candidates = [row for row in rows if row['gap_count'] and row['state'] != 'closed'][:100]
    cache = directory / 'decisions' / (input_digest + '.json')
    order = _decision(cache, candidates, ranker) if ranker.mode == 'model' else None
    reason = 'decision_cached'
    if order is None:
        order, reason = ranker.rank(candidates)
        if not valid_order({'order': order}, len(candidates)) or reason not in RANK_REASONS:
            order, reason = list(range(len(candidates))), 'fallback_model_invalid'
        if reason == 'model_ranked':
            decision = {'policy_version': POLICY, 'model': ranker.model,
                        'order': [dict(zip(('venue_id', 'venue_key'), identity(candidates[i]))) for i in order]}
            decision['decision_digest'] = digest(decision)
            _write(cache, decision)
    rows = _apply_rank(rows, candidates, order)
    decision = {'policy_version': POLICY, 'model': ranker.model,
                'order': [dict(zip(('venue_id', 'venue_key'), identity(candidates[i]))) for i in order]}
    actual_mode = 'model' if reason in ('model_ranked', 'decision_cached') else 'local'
    ranking_info = {'mode': actual_mode, 'model': ranker.model, 'reason': reason,
                    'decision_digest': digest(decision),
                    'candidate_count': len(candidates)}
    rank_positions = {identity(row): i for i, row in enumerate(rows)}
    web, by_id = [], {}
    for target in sorted(targets, key=lambda t: (rank_positions[identity(t)], target_id(t))):
        tid = target_id(target)
        if tid in by_id:
            by_id[tid]['occurrences'] += 1
        else:
            entry = {'target_id': tid, 'venue_id': target['venue_id'], 'venue_key': target['venue_key'],
                     'urls': target['urls'], 'occurrences': 1}
            by_id[tid] = entry
            web.append(entry)
    read_list, handles, venues = [], set(), set()
    # The checkpoint is scoped to today. A plan for another day starts from
    # that day's fresh quota and must not inherit today's count or halt.
    if day == now.date().isoformat():
        quota = min(IG_READ_MAX, max(0, IG_READ_MAX - state.data['counts']['ig_reads']))
        if state.halted():
            quota = 0
            notes['ig_halted_today'] += 1
    else:
        quota = IG_READ_MAX
    if now >= datetime.fromisoformat(window(day)['end']):
        notes['ig_window_passed'] += 1
    for watch in sorted(watchlist, key=lambda w: (rank_positions[identity(w)], w['handle'].lower())):
        row = rows[rank_positions[identity(watch)]]
        handle = watch['handle'].lower()
        if (len(read_list) >= quota or not row['gap_count'] or row['state'] == 'closed'
                or row['identity_status'] != 'resolved' or identity(row) in venues or handle in handles):
            continue
        read_list.append({'rank': len(read_list) + 1, 'venue_id': row['venue_id'],
                          'venue_key': row['venue_key'], 'handle': handle, 'name': watch['name'],
                          'gap_count': row['gap_count'], 'fields': [f['field_id'] for f in row['fields']]})
        handles.add(handle)
        venues.add(identity(row))
    plan = {'schema_version': 1, 'input_digest': input_digest, 'policy_version': POLICY,
            'target_set_digest': target_digest, 'targets_status': targets_status,
            'day': day, 'timezone': 'Asia/Seoul', 'generated_at': now.isoformat(),
            'ranking': ranking_info, 'ranked_venues': rows, 'web_targets': web,
            'ig_window': window(day), 'ig_read_list': read_list, 'diagnostics': _diagnostics(notes),
            'auto_publish_to_site': False}
    plan['plan_id'] = digest(plan)
    if not validate_plan(plan, day):
        raise CollectError('plan_invalid')
    _write(path, plan)
    marker.unlink(missing_ok=True)
    return plan


def order_targets(targets, plan):
    """Replay order against current config only; never execute plan URLs."""
    notes = Counter()
    current = {target_id(t) for t in targets}
    positions = {row['target_id']: i for i, row in enumerate(plan['web_targets'])}
    notes['removed_targets'] = len(set(positions) - current)
    notes['unplanned_targets'] = len(current - set(positions))
    if plan['target_set_digest'] != target_set_digest(targets):
        notes['targets_changed'] = 1
    # Caller supplies canonical local order for append-only new targets.
    return sorted(targets, key=lambda t: positions.get(target_id(t), len(positions))), _diagnostics(notes)


def local_target_order(settings, targets, *, now, materialized=None):
    rows, _, _ = materialized if materialized is not None else snapshot(settings, targets, [], now=now)
    positions = {identity(row): index for index, row in enumerate(rows)}
    return sorted(targets, key=lambda t: (positions[identity(t)], target_id(t)))


def run_plan(settings, *, ranking='model', clock=None, planner=None, planner_session=None, testing=False):
    clock = clock or Clock()
    now = kst_now(clock)
    day = (now.date() + timedelta(days=now.hour >= 14)).isoformat()
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.data_dir.chmod(0o700)
        with ProcessLock(settings.data_dir / 'daemon.lock'):
            state = State(settings.data_dir, clock)
            assembled = assemble_targets(settings, now=now, testing=testing)
            targets, status = assembled['targets'], assembled['targets_status']
            plan = ensure_daily_plan(settings, state, targets, status, clock=clock, day=day,
                                     planner=planner, planner_session=planner_session, testing=testing, ranking=ranking,
                                     assembled=assembled)
            # The selected day's window is always in the future, so a late run
            # reports what it prepared rather than a passed window.
            reason = 'prepared_tomorrow' if now.hour >= 14 else 'ok'
            result = {'state': 'done', 'reason': reason, 'day': day,
                    'plan_path': redact(str(settings.data_dir / 'plans' / (day + '.json'))),
                    'ranked_count': len(plan['ranked_venues']), 'web_target_count': len(plan['web_targets']),
                    'ig_list_count': len(plan['ig_read_list']), 'ranking_mode': plan['ranking']['mode'],
                    'ranking_reason': plan['ranking']['reason'], 'auto_publish_to_site': False,
                    # Transient per-run diagnostics; plan artifacts keep the DeepSeek id.
                    'provider': dict(state.model_providers)}
            if state.primary_skipped == 'insufficient_balance':
                result['primary_skipped'] = 'insufficient_balance'
            return result
    except AlreadyRunning:
        return {'state': 'already_running', 'reason': 'already_running', 'auto_publish_to_site': False}
    except CollectError as error:
        return {'state': 'error', 'reason': error.reason, 'auto_publish_to_site': False}
    except (OSError, ValueError, TypeError):
        return {'state': 'error', 'reason': 'plan_invalid', 'auto_publish_to_site': False}
