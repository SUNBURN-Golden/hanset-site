"""Durable collector accounting and work metadata. No provider or page bodies."""
import os
import sqlite3
from contextlib import closing
from datetime import timedelta

from hanset_active.util.timeutil import kst_now

from . import CollectError
from .config import outside

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS provider_health (
    provider TEXT PRIMARY KEY, observed_at TEXT NOT NULL, outcome TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_usage (
    attempt_id INTEGER PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('completion','probe')),
    dispatched_at TEXT NOT NULL, outcome TEXT NOT NULL, input_chars INTEGER NOT NULL,
    max_output_tokens INTEGER NOT NULL, prompt_tokens INTEGER, completion_tokens INTEGER,
    cost_basis TEXT NOT NULL DEFAULT 'unknown', cost_amount REAL, cost_currency TEXT
);
CREATE TABLE IF NOT EXISTS extraction_cache (
    source_kind TEXT NOT NULL, requested_url TEXT NOT NULL, content_hash TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    PRIMARY KEY (source_kind, requested_url, content_hash)
);
CREATE TABLE IF NOT EXISTS web_completions (
    day TEXT NOT NULL, requested_url TEXT NOT NULL, disposition TEXT NOT NULL,
    PRIMARY KEY (day, requested_url)
);
CREATE TABLE IF NOT EXISTS extraction_retries (
    requested_url TEXT NOT NULL, venue_id TEXT NOT NULL, venue_key TEXT NOT NULL,
    content_hash TEXT, reason TEXT NOT NULL, retry_at TEXT NOT NULL,
    parent_url TEXT, fetch_url TEXT NOT NULL,
    PRIMARY KEY (requested_url, venue_id, venue_key)
);
'''


def connect(directory):
    directory = outside(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / 'collect-runtime.sqlite3'
    if path.is_symlink():
        raise CollectError('state_corrupt')
    db = sqlite3.connect(path, timeout=30)
    try:
        os.chmod(path, 0o600)
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(_SCHEMA)
        return db
    except BaseException:
        db.close()
        raise


def health(db, provider, clock):
    at = kst_now(clock)
    row = db.execute('''SELECT outcome FROM provider_health
                         WHERE provider=? AND observed_at>? AND observed_at<=?''',
                     (provider, (at - timedelta(hours=6)).isoformat(), at.isoformat())).fetchone()
    return row[0] if row else None


def remember_health(db, provider, clock, outcome):
    with db:
        db.execute('''INSERT INTO provider_health VALUES (?,?,?)
                      ON CONFLICT(provider) DO UPDATE SET observed_at=excluded.observed_at,
                      outcome=excluded.outcome''', (provider, kst_now(clock).isoformat(), outcome))


def dispatched(db, provider, model, kind, clock, chars, output):
    with db:
        return db.execute('''INSERT INTO model_usage
            (provider,model,kind,dispatched_at,outcome,input_chars,max_output_tokens)
            VALUES (?,?,?,?,'dispatched',?,?)''',
            (provider, model, kind, kst_now(clock).isoformat(), chars, output)).lastrowid


def finished(db, attempt, outcome, *, prompt=None, completion=None, cost=None, currency=None):
    with db:
        db.execute('''UPDATE model_usage SET outcome=?,prompt_tokens=?,completion_tokens=?,
                      cost_basis=?,cost_amount=?,cost_currency=? WHERE attempt_id=?''',
                   (outcome, prompt, completion, 'provider_reported' if cost is not None else 'unknown',
                    cost, currency, attempt))


def invalid_output(directory, attempt):
    if attempt is not None:
        with closing(connect(directory)) as db, db:
            db.execute("UPDATE model_usage SET outcome='model_invalid' WHERE attempt_id=?", (attempt,))


def web_completed(db, state, *, testing):
    from .targets import canonical_requested_url

    day = state.data['day']
    imported = []
    for url, disposition in tuple(state.data['completed'].items()):
        if url.startswith('https://www.instagram.com/'):
            continue
        try:
            key = canonical_requested_url(url, testing=testing)
        except ValueError:
            continue
        if not disposition.replace('_', '').isalnum():
            disposition = 'error'
        imported.append((day, key, disposition))
        del state.data['completed'][url]
    if imported:
        with db:
            db.executemany('INSERT OR IGNORE INTO web_completions VALUES (?,?,?)', imported)
        state.save()
    return {row[0] for row in db.execute('SELECT requested_url FROM web_completions WHERE day=?', (day,))}


def complete_web(db, day, key, disposition):
    with db:
        db.execute('INSERT OR REPLACE INTO web_completions VALUES (?,?,?)', (day, key, disposition))
        db.execute('DELETE FROM extraction_retries WHERE requested_url=?', (key,))


def retry_web(db, row, clock, reason, *, delay=0):
    from hanset_active.util.urls import canonicalize

    fetch_url = canonicalize(row['url'], preserve_path=True)
    with db:
        db.execute('''INSERT OR REPLACE INTO extraction_retries
            (requested_url, venue_id, venue_key, content_hash, reason, retry_at, parent_url, fetch_url)
            VALUES (?,?,?,?,?,?,?,?)''',
            (row['requested_url'], row['venue_id'], row['venue_key'], row.get('content_hash'), reason,
             (kst_now(clock) + timedelta(seconds=delay)).isoformat(), row.get('parent_url'), fetch_url))
