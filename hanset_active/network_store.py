"""Collector-owned network state, independent of active authorization."""
import os
import sqlite3

from .config import REPO_ROOT

AUDIT_FILE = 'collect-fetch.sqlite3'
LEASE_FILE = 'collect-leases.sqlite3'

_AUDIT_SCHEMA = '''
CREATE TABLE IF NOT EXISTS request_log (
    request_id INTEGER PRIMARY KEY, url TEXT, domain TEXT, ts TEXT, status INTEGER,
    kind TEXT, bytes INTEGER, outcome TEXT, run_id TEXT, adapter TEXT,
    transfer_complete INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS fetch_log (
    id INTEGER PRIMARY KEY, url TEXT, domain TEXT, ts TEXT, status INTEGER, kind TEXT, bytes INTEGER
);
CREATE TABLE IF NOT EXISTS failures (
    failure_id TEXT PRIMARY KEY, task_id TEXT, url TEXT, domain TEXT, reason TEXT, state TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY, domain TEXT NOT NULL UNIQUE, state TEXT, reason TEXT, next_at TEXT
);
CREATE TABLE IF NOT EXISTS host_timing (
    host TEXT PRIMARY KEY, requested_at TEXT, crawl_delay REAL NOT NULL DEFAULT 0,
    robots_attempt TEXT
);
CREATE TABLE IF NOT EXISTS news_robots (
    origin TEXT PRIMARY KEY, body TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS host_backoff (host TEXT PRIMARY KEY, retry_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resource_blocks (
    url_hash TEXT PRIMARY KEY, struck_at TEXT NOT NULL, hold_until TEXT
);
'''

_LEASE_SCHEMA = '''CREATE TABLE IF NOT EXISTS run_lease (
    lease_key TEXT PRIMARY KEY, run_id TEXT, heartbeat TEXT, lease_until TEXT
);'''


def _open(directory, name, schema):
    directory = directory.resolve()
    if directory == REPO_ROOT or REPO_ROOT in directory.parents:
        raise ValueError('Database must be outside checkout')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / name
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    db = sqlite3.connect(path, timeout=30, check_same_thread=False)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(schema)
        return db
    except BaseException:
        db.close()
        raise


def open_audit(directory):
    return _open(directory, AUDIT_FILE, _AUDIT_SCHEMA)


def open_leases(directory):
    return _open(directory, LEASE_FILE, _LEASE_SCHEMA)
