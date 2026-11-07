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
    FOREIGN KEY (receipt_id) REFERENCES receipts(receipt_id)
);
'''


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
    os.chmod(path, 0o600)
    return db


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
        stored_as = 'applied' if kept else 'empty_extraction'
        db.execute(
            'INSERT INTO receipts(receipt_id,source_kind,source_url,content_hash,disposition,created_at) VALUES (?,?,?,?,?,?)',
            (receipt_id, kind, url, text_hash, stored_as, kst_now(clock).isoformat()))
        for claim_id, claim in kept:
            db.execute(
                '''INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (claim_id, receipt_id, venue_id, venue_key, claim['field'],
                 json.dumps(claim['value'], ensure_ascii=False, sort_keys=True), claim['quote'][:240],
                 url, kind, source_type, 'source_claim', 'internal_review_only', claim['rule_id'],
                 seen_at, text_hash))
        db.commit()
        return stored_as, [claim_id for claim_id, _ in kept]
    except Exception:
        db.rollback()
        raise
