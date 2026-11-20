"""Collector claims live outside the checkout and outside the site/twin stores."""
import hashlib
import json
import os
import re
import sqlite3
import struct
from datetime import datetime, timedelta
from functools import cache

from hanset_active.db import deterministic_id
from hanset_active.util.timeutil import kst_now

from . import CollectError
from .config import BLOCK_COOLDOWN_DAYS, UNCHANGED_COOLDOWN_DAYS, outside
from .ground import secrets

SCHEMA = '''
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('web', 'instagram')),
    source_url TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    disposition TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source_kind, source_url, content_hash)
);
CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    venue_key TEXT NOT NULL,
    field TEXT NOT NULL,
    value_json TEXT NOT NULL,
    quote TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status = 'source_claim'),
    rights TEXT NOT NULL CHECK (rights = 'internal_review_only'),
    rule_id TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    evidence_origin TEXT,
    evidence_role TEXT,
    verbatim_source INTEGER,
    FOREIGN KEY (receipt_id) REFERENCES receipts(receipt_id),
    CHECK (
        (evidence_origin = 'web_verbatim' AND evidence_role = 'page_span' AND verbatim_source = 1)
        OR (evidence_origin = 'operator_summary' AND evidence_role = 'summary_span' AND verbatim_source = 0)
    )
);
'''
CANDIDATE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS fact_candidates (
    candidate_id TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL,
    venue_key TEXT NOT NULL,
    field TEXT NOT NULL,
    qualifiers_json TEXT NOT NULL,
    normalized_value_json TEXT NOT NULL,
    normalizer_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status = 'fact_candidate'),
    rights TEXT NOT NULL CHECK (rights = 'internal_review_only'),
    review_state TEXT NOT NULL CHECK (review_state IN
        ('awaiting_review', 'needs_confirmation', 'evidence_hold')),
    assessment_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    changed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_candidate_claims (
    candidate_id TEXT NOT NULL REFERENCES fact_candidates(candidate_id),
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    PRIMARY KEY (candidate_id, claim_id)
);
CREATE INDEX IF NOT EXISTS fact_candidates_review ON fact_candidates(review_state);
CREATE INDEX IF NOT EXISTS fact_candidate_claims_claim ON fact_candidate_claims(claim_id);
'''
_PROVENANCE = {
    'web': ('web_verbatim', 'page_span', 1),
    'instagram': ('operator_summary', 'summary_span', 0),
}

# Additive internal review storage; existing claims are never relabeled.
CHANGE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS web_change_observations (
    observation_id TEXT PRIMARY KEY,
    observed_seq INTEGER NOT NULL UNIQUE,
    day TEXT NOT NULL,
    venue_id TEXT NOT NULL, venue_key TEXT NOT NULL,
    source_identity TEXT NOT NULL, target_fingerprint TEXT NOT NULL,
    requested_url TEXT NOT NULL, final_url TEXT NOT NULL,
    content_hash TEXT NOT NULL, observed_at TEXT NOT NULL,
    values_json TEXT NOT NULL,
    detector_version TEXT NOT NULL, normalizer_version TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0 CHECK (processed IN (0,1,2)),
    diagnostic TEXT,
    UNIQUE (day, venue_id, venue_key, source_identity, content_hash, target_fingerprint)
);
CREATE TABLE IF NOT EXISTS web_change_baselines (
    venue_id TEXT NOT NULL, venue_key TEXT NOT NULL,
    field TEXT NOT NULL, qualifier_key TEXT NOT NULL,
    source_identity TEXT NOT NULL, target_fingerprint TEXT NOT NULL,
    value_json TEXT NOT NULL, comparison_key TEXT NOT NULL,
    value_kind TEXT NOT NULL CHECK (value_kind IN ('observation','bootstrap_claim')),
    source_observation_id TEXT, source_claim_id TEXT,
    observed_at TEXT NOT NULL, observed_seq INTEGER NOT NULL,
    detector_version TEXT NOT NULL, normalizer_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active','needs_rebaseline')),
    last_event_id TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (venue_id, venue_key, field, qualifier_key, source_identity, target_fingerprint),
    CHECK ((source_observation_id IS NOT NULL) != (source_claim_id IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS web_change_events (
    change_id TEXT PRIMARY KEY, event_seq INTEGER NOT NULL UNIQUE,
    event_kind TEXT NOT NULL CHECK (event_kind IN
      ('hours','price_day_pass','closures','qualifier_added','qualifier_missing','authorization_changed')),
    venue_id TEXT NOT NULL, venue_key TEXT NOT NULL,
    field TEXT NOT NULL, qualifier_key TEXT NOT NULL,
    source_identity TEXT NOT NULL, target_fingerprint TEXT NOT NULL,
    requested_url TEXT NOT NULL, final_url TEXT NOT NULL,
    old_value_json TEXT, new_value_json TEXT,
    old_comparison_key TEXT, new_comparison_key TEXT,
    old_observation_id TEXT, new_observation_id TEXT,
    predecessor_observation_id TEXT, predecessor_claim_id TEXT,
    observed_at TEXT NOT NULL, checked TEXT NOT NULL,
    detector_version TEXT NOT NULL, normalizer_version TEXT NOT NULL,
    review_reason TEXT, usable_for_viral INTEGER NOT NULL CHECK (usable_for_viral IN (0,1)),
    rights TEXT NOT NULL CHECK (rights='internal_review_only'),
    auto_publish_to_site INTEGER NOT NULL CHECK (auto_publish_to_site=0),
    created_at TEXT NOT NULL,
    CHECK (event_kind NOT IN ('hours','price_day_pass','closures') OR
      ((predecessor_observation_id IS NOT NULL) != (predecessor_claim_id IS NOT NULL)))
);
CREATE TRIGGER IF NOT EXISTS web_change_events_immutable_update BEFORE UPDATE ON web_change_events
BEGIN SELECT RAISE(ABORT, 'immutable_event'); END;
CREATE TRIGGER IF NOT EXISTS web_change_events_immutable_delete BEFORE DELETE ON web_change_events
BEGIN SELECT RAISE(ABORT, 'immutable_event'); END;
CREATE TABLE IF NOT EXISTS web_change_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS web_change_pending ON web_change_observations(processed,observed_seq);
'''

BLOCK_SCHEMA = '''
CREATE TABLE IF NOT EXISTS web_target_blocks (
    url_hash TEXT PRIMARY KEY,
    disposition TEXT NOT NULL CHECK (disposition IN ('robots_disallowed','login_wall','blocked')),
    blocked_at TEXT NOT NULL
);
'''
_BLOCK_DISPOSITIONS = frozenset({'robots_disallowed', 'login_wall', 'blocked'})

WEB_TARGET_SCHEMA = '''
CREATE TABLE IF NOT EXISTS web_target_observations (
    url_hash TEXT PRIMARY KEY,
    content_hash TEXT,
    disposition TEXT NOT NULL CHECK (disposition IN
      ('applied','duplicate','empty_extraction','pending_extraction',
       'robots_disallowed','login_wall','blocked','empty_body','error',
       'excluded','denied','asset_document','off_domain','policy_page')),
    observed_at TEXT NOT NULL
);
'''
_HASH = re.compile(r'[0-9a-f]{64}')


def _valid_hash(value):
    return isinstance(value, str) and bool(_HASH.fullmatch(value))


def connect(directory):
    directory = outside(directory)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    path = directory / 'collector.sqlite3'
    if path.is_symlink():
        raise CollectError('state_corrupt')
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(SCHEMA)
    _migrate(db)
    db.executescript(CANDIDATE_SCHEMA)
    db.executescript(CHANGE_SCHEMA)
    db.executescript(BLOCK_SCHEMA)
    db.executescript(WEB_TARGET_SCHEMA)
    os.chmod(path, 0o600)
    return db


def _migrate(db):
    """Add provenance columns. Existing rows stay NULL and are not relabeled."""
    present = {row[1] for row in db.execute('PRAGMA table_info(claims)')}
    columns = {
        'evidence_origin': 'TEXT',
        'evidence_role': 'TEXT',
        'verbatim_source': 'INTEGER',
    }
    for name, declared in columns.items():
        if name not in present:
            db.execute(f'ALTER TABLE claims ADD COLUMN {name} {declared}')
    # Inserts after this point must name an origin. Existing NULL rows are not updated.
    db.execute('''
        CREATE TRIGGER IF NOT EXISTS claims_provenance_required
        BEFORE INSERT ON claims
        FOR EACH ROW
        WHEN NEW.evidence_origin IS NULL
          OR NEW.evidence_role IS NULL
          OR NEW.verbatim_source IS NULL
          OR NEW.evidence_origin NOT IN ('web_verbatim', 'operator_summary')
          OR (NEW.evidence_origin = 'web_verbatim' AND NOT (NEW.evidence_role = 'page_span' AND NEW.verbatim_source = 1))
          OR (NEW.evidence_origin = 'operator_summary' AND NOT (NEW.evidence_role = 'summary_span' AND NEW.verbatim_source = 0))
        BEGIN
            SELECT RAISE(ABORT, 'provenance_required');
        END
    ''')
    db.commit()


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _block_expiry(row):
    try:
        at = datetime.fromisoformat(row['blocked_at'])
        if (at.utcoffset() is None or row['disposition'] not in _BLOCK_DISPOSITIONS
                or not re.fullmatch(r'[0-9a-f]{64}', row['url_hash'])):
            raise ValueError
        return at + timedelta(days=BLOCK_COOLDOWN_DAYS)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise CollectError('state_corrupt') from None


def _prune_web_target_blocks(db, now):
    # Parse before deleting: malformed state must never silently bypass a hold.
    expired = []
    for row in db.execute('SELECT * FROM web_target_blocks'):
        if _block_expiry(row) <= now:
            expired.append((row['url_hash'],))
    db.executemany('DELETE FROM web_target_blocks WHERE url_hash=?', expired)


def prune_web_target_blocks(db, *, clock=None):
    with db:
        _prune_web_target_blocks(db, kst_now(clock))


def recent_web_target_block(db, url, *, clock=None, testing=False):
    from .targets import canonical_requested_url
    key = digest(canonical_requested_url(url, testing=testing))
    row = db.execute('SELECT * FROM web_target_blocks WHERE url_hash=?', (key,)).fetchone()
    return bool(row and kst_now(clock) < _block_expiry(row))


def remember_web_target_block(db, url, disposition, *, status, clock=None, testing=False):
    """Only per-URL evaluations establish holds; collateral domain stops do not."""
    if disposition not in _BLOCK_DISPOSITIONS or (disposition != 'robots_disallowed' and status is None):
        return
    from .targets import canonical_requested_url
    key = digest(canonical_requested_url(url, testing=testing))
    now = kst_now(clock)
    with db:
        _prune_web_target_blocks(db, now)
        db.execute('''INSERT INTO web_target_blocks(url_hash,disposition,blocked_at) VALUES (?,?,?)
                      ON CONFLICT(url_hash) DO UPDATE SET
                      disposition=excluded.disposition,blocked_at=excluded.blocked_at''',
                   (key, disposition, now.isoformat()))


def _observation_time(value):
    try:
        at = datetime.fromisoformat(value)
        return at if at.utcoffset() is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def remember_web_target_observation(db, url, disposition, *, observed_at, content_hash=None,
                                    testing=False):
    """Scheduling metadata only; never refresh or relabel historical evidence."""
    from .targets import canonical_requested_url
    key = digest(canonical_requested_url(url, testing=testing))
    at = _observation_time(observed_at)
    if at is None or (content_hash is not None and not _valid_hash(content_hash)):
        raise CollectError('state_corrupt')
    with db:
        db.execute('''INSERT INTO web_target_observations
                        (url_hash,content_hash,disposition,observed_at) VALUES (?,?,?,?)
                      ON CONFLICT(url_hash) DO UPDATE SET
                        content_hash=excluded.content_hash,disposition=excluded.disposition,
                        observed_at=excluded.observed_at''',
                   (key, content_hash, disposition, at.isoformat()))


def web_target_history(db, *, clock=None, testing=False):
    """Return last fetch times and unchanged deferrals by canonical URL hash."""
    from .targets import canonical_requested_url
    @cache
    def safe_key(url):
        try:
            return digest(canonical_requested_url(url, testing=testing))
        except (ValueError, TypeError, UnicodeError):
            return None

    scheduling = {row['url_hash']: (row['content_hash'], row['disposition'],
                                   _observation_time(row['observed_at']))
                  for row in db.execute('SELECT * FROM web_target_observations')}
    history, receipts, applied = {}, {}, set()

    def latest(key, content_hash, disposition, at):
        if key not in history or at >= history[key][2]:
            history[key] = (content_hash, disposition, at)

    for row in db.execute(
            "SELECT source_url,content_hash,disposition,created_at FROM receipts "
            "WHERE source_kind='web' ORDER BY rowid"):
        key = safe_key(row['source_url'])
        at = _observation_time(row['created_at'])
        if key is None or at is None or not _valid_hash(row['content_hash']):
            continue
        receipt_key = (key, row['content_hash'])
        if row['disposition'] == 'applied':
            applied.add(receipt_key)
        if key not in scheduling:
            if receipt_key not in receipts or at >= receipts[receipt_key][1]:
                receipts[receipt_key] = (row['disposition'], at)
            latest(key, row['content_hash'], row['disposition'], at)
    for row in db.execute(
            'SELECT requested_url,content_hash,observed_at FROM web_change_observations ORDER BY observed_seq'):
        key = safe_key(row['requested_url'])
        at = _observation_time(row['observed_at'])
        if key is not None and key not in scheduling and at is not None:
            disposition, _ = receipts.get((key, row['content_hash']), (None, None))
            latest(key, row['content_hash'], disposition, at)
    history.update(scheduling)
    now = kst_now(clock)
    last_fetched = {key: at for key, (_, _, at) in history.items() if at is not None and at <= now}
    unchanged = {key for key, (content_hash, disposition, _) in history.items()
                 if key in last_fetched and now - last_fetched[key] < timedelta(days=UNCHANGED_COOLDOWN_DAYS)
                 and (disposition in {'duplicate', 'empty_extraction'}
                      or (disposition == 'applied' and (key, content_hash) in applied))}
    return last_fetched, unchanged


def recent_unchanged_web_targets(db, *, clock=None, testing=False):
    return web_target_history(db, clock=clock, testing=testing)[1]


def _clean(value):
    blob = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return not any(secret and secret in blob for secret in secrets())


def record(db, *, kind, url, text_hash, disposition, claims, venue_id, venue_key, seen_at,
           clock=None, source_type=None):
    """Insert one receipt and its grounded claims, or return the existing receipt."""
    if kind not in ('web', 'instagram'):
        raise CollectError('runtime_error')
    if source_type is None:
        source_type = 'own_site' if kind == 'web' else 'sns'
    if source_type not in ({'own_site', 'third_party'} if kind == 'web' else {'sns'}):
        raise CollectError('runtime_error')
    db.execute('BEGIN IMMEDIATE')
    try:
        existing = db.execute(
            'SELECT disposition FROM receipts WHERE source_kind=? AND source_url=? AND content_hash=?',
            (kind, url, text_hash)).fetchone()
        if existing:
            db.commit()
            return 'duplicate', []
        receipt_id = deterministic_id('collector-receipt', kind, url, text_hash)
        kept = []
        for claim in claims:
            if not _clean(claim['value']) or not _clean(claim['quote']) or len(claim['quote']) > 240:
                continue
            claim_id = deterministic_id('collector-claim', receipt_id, claim['field'], claim['value'])
            kept.append((claim_id, claim))
        origin, role, verbatim = _PROVENANCE[kind]
        stored_as = 'applied' if kept else 'empty_extraction'
        db.execute(
            'INSERT INTO receipts(receipt_id,source_kind,source_url,content_hash,disposition,created_at) VALUES (?,?,?,?,?,?)',
            (receipt_id, kind, url, text_hash, stored_as, kst_now(clock).isoformat()))
        for claim_id, claim in kept:
            db.execute(
                '''INSERT INTO claims (
                    claim_id, receipt_id, venue_id, venue_key, field, value_json, quote,
                    source_url, source_kind, source_type, status, rights, rule_id, seen_at,
                    content_hash, evidence_origin, evidence_role, verbatim_source
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (claim_id, receipt_id, venue_id, venue_key, claim['field'],
                 json.dumps(claim['value'], ensure_ascii=False, sort_keys=True), claim['quote'][:240],
                 url, kind, source_type, 'source_claim', 'internal_review_only', claim['rule_id'],
                 seen_at, text_hash, origin, role, verbatim))
        db.commit()
        return stored_as, [claim_id for claim_id, _ in kept]
    except Exception:
        db.rollback()
        raise


def read_claims(db):
    """Read stored claims across schemas without relabeling NULL provenance."""
    if not db or not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims'").fetchone():
        return []
    rows = db.execute('SELECT * FROM claims ORDER BY venue_key, field, seen_at, claim_id').fetchall()
    claims = []
    for row in rows:
        row = dict(row)
        for name in ('evidence_origin', 'evidence_role', 'verbatim_source'):
            row.setdefault(name, None)
        verbatim = None if row['verbatim_source'] is None else int(row['verbatim_source'])
        claims.append({
            'claim_id': row['claim_id'],
            'venue_id': row['venue_id'],
            'venue_key': row['venue_key'],
            'field': row['field'],
            'value': json.loads(row['value_json']),
            'quote': row['quote'],
            'source_url': row['source_url'],
            'source_kind': row['source_kind'],
            'source_type': row['source_type'],
            'status': row['status'],
            'rights': row['rights'],
            'seen_at': row['seen_at'],
            'content_hash': row['content_hash'],
            'evidence_origin': row['evidence_origin'],
            'evidence_role': row['evidence_role'],
            'verbatim_source': verbatim,
            'original_caption': False,
        })
    return claims


def evidence_report(db):
    """Compatible evidence view; no implicit approvals or persistence."""
    from .promotion import assess
    return assess(read_claims(db), stored_candidates(db),
                  {'approvals': [], 'fingerprint': None}, now=kst_now())


def connect_readonly(directory):
    """In-memory committed snapshot; even WAL read marks stay untouched.

    A normal mode=ro SQLite connection may create/update -shm. Reading stable
    bytes and replaying committed WAL frames avoids that dry-run side effect.
    See https://sqlite.org/fileformat2.html#walformat (checksums and commits).
    """
    directory = outside(directory)
    path = directory / 'collector.sqlite3'
    if path.is_symlink():
        raise CollectError('state_corrupt')
    if not path.exists():
        return None
    image = _readonly_image(path)
    db = sqlite3.connect(':memory:')
    try:
        db.deserialize(image)
    except Exception:
        db.close()
        raise
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def _readonly_image(path):
    wal_path = path.with_name(path.name + '-wal')
    journal_path = path.with_name(path.name + '-journal')

    def read_optional(file):
        if file.is_symlink():
            raise CollectError('state_corrupt')
        try:
            return file.read_bytes()
        except FileNotFoundError:
            return b''

    for _ in range(3):
        main, wal, journal = path.read_bytes(), read_optional(wal_path), read_optional(journal_path)
        if (main == path.read_bytes() and wal == read_optional(wal_path)
                and journal == read_optional(journal_path)):
            break
    else:
        raise CollectError('state_corrupt')
    # Legacy hot-journal recovery requires a writable operator open, not report.
    if journal[:8] == bytes.fromhex('d9d505f920a163d7'):
        raise CollectError('state_corrupt')
    if len(main) < 100 or main[:16] != b'SQLite format 3\x00':
        raise CollectError('state_corrupt')
    page_size = int.from_bytes(main[16:18], 'big')
    page_size = 65536 if page_size == 1 else page_size
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1) or len(main) % page_size:
        raise CollectError('state_corrupt')
    image = bytearray(main)
    if wal:
        if len(wal) < 32:
            raise CollectError('state_corrupt')
        magic, version, wal_size = struct.unpack('>III', wal[:12])
        if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or wal_size != page_size:
            raise CollectError('state_corrupt')
        endian = '<' if magic == 0x377f0682 else '>'

        def checksum(data, state=(0, 0)):
            a, b = state
            for x, y in struct.iter_unpack(endian + 'II', data):
                a = (a + x + b) & 0xffffffff
                b = (b + y + a) & 0xffffffff
            return a, b

        state = checksum(wal[:24])
        if state != struct.unpack('>II', wal[24:32]):
            raise CollectError('state_corrupt')
        frames, committed, pages = [], 0, 0
        for offset in range(32, len(wal) - page_size - 23, page_size + 24):
            header = wal[offset:offset+24]
            content = wal[offset+24:offset+24+page_size]
            page, size = struct.unpack('>II', header[:8])
            current = checksum(content, checksum(header[:8], state))
            if header[8:16] != wal[16:24] or current != struct.unpack('>II', header[16:24]):
                break  # Uncommitted tail or frames from a prior WAL generation.
            if page == 0:
                raise CollectError('state_corrupt')
            state = current
            frames.append((page, content))
            if size:
                committed, pages = len(frames), size
        if committed:
            # Bound allocation by actual pages available, including growth frames.
            if pages > len(main)//page_size + committed:
                raise CollectError('state_corrupt')
            image = image[:pages * page_size]
            image.extend(b'\x00' * (pages * page_size - len(image)))
            for page, content in frames[:committed]:
                if page <= pages:
                    image[(page-1)*page_size:page*page_size] = content
    image[18:20] = b'\x01\x01'  # Only the in-memory copy uses rollback mode.
    return bytes(image)


def stored_candidates(db):
    if not db or not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fact_candidates'").fetchone():
        return []
    out = []
    for row in db.execute('SELECT * FROM fact_candidates ORDER BY candidate_id'):
        item = dict(row)
        item['qualifiers'] = json.loads(item.pop('qualifiers_json'))
        item['normalized_value'] = json.loads(item.pop('normalized_value_json'))
        item['supporting_claim_ids'] = [link[0] for link in db.execute(
            'SELECT claim_id FROM fact_candidate_claims WHERE candidate_id=? ORDER BY claim_id', (item['candidate_id'],))]
        out.append(item)
    return out


def persist_candidates(db, candidates, *, at):
    """Caller owns the write transaction and process lock. Links are append only."""
    from .normalize import stable_json
    changed = 0
    for item in candidates:
        params = (item['candidate_id'], item['venue_id'], item['venue_key'], item['field'],
                  stable_json(item['qualifiers']), stable_json(item['normalized_value']),
                  item['normalizer_version'], 'fact_candidate', 'internal_review_only',
                  item['review_state'], item['assessment_digest'], at, at)
        result = db.execute('''INSERT INTO fact_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(candidate_id) DO UPDATE SET
                review_state=excluded.review_state,
                assessment_digest=excluded.assessment_digest,
                changed_at=excluded.changed_at
            WHERE fact_candidates.review_state != excluded.review_state
               OR fact_candidates.assessment_digest != excluded.assessment_digest''', params)
        changed += result.rowcount
        if result.rowcount:
            item.setdefault('created_at', at)
            item['changed_at'] = at
        for claim_id in item['supporting_claim_ids']:
            db.execute('INSERT OR IGNORE INTO fact_candidate_claims VALUES (?,?)', (item['candidate_id'], claim_id))
    return changed
