"""Collector claims live outside the checkout and outside the site/twin stores."""
import hashlib
import json
import os
import sqlite3

from hanset_active.db import deterministic_id
from hanset_active.util.timeutil import kst_now

from . import CollectError
from .config import outside
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


def _clean(value):
    blob = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return not any(secret and secret in blob for secret in secrets())


def record(db, *, kind, url, text_hash, disposition, claims, venue_id, venue_key, seen_at, clock=None):
    """Insert one receipt and its grounded claims, or return the existing receipt."""
    if kind not in ('web', 'instagram'):
        raise CollectError('runtime_error')
    source_type = 'own_site' if kind == 'web' else 'sns'
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
    """No mkdir, chmod, WAL initialization or migrations, including old schemas."""
    directory = outside(directory)
    path = directory / 'collector.sqlite3'
    if path.is_symlink():
        raise CollectError('state_corrupt')
    if not path.exists():
        return None
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


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
