"""External SQLite state with atomic reservations and append-only observations."""
import contextlib
import os
import time
import sqlite3
from .config import external_path, state_path

SQL = '''
CREATE TABLE IF NOT EXISTS counters(day TEXT, brand TEXT, candidates INTEGER DEFAULT 0,
 new_bodies INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0, PRIMARY KEY(day,brand));
CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, brand TEXT, url TEXT, channel TEXT,
 stage TEXT, data TEXT, UNIQUE(brand,url));
CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY, clip_id TEXT UNIQUE, record_id TEXT,
 brand TEXT, day TEXT, url TEXT, document_id TEXT, channel TEXT, generation INTEGER,
 domain_epoch INTEGER, domain TEXT, state TEXT, issued REAL, expires REAL,
 worker TEXT, payload_hash TEXT, ack TEXT, request_count INTEGER DEFAULT 0,
 byte_count INTEGER DEFAULT 0, FOREIGN KEY(record_id) REFERENCES records(id));
CREATE TABLE IF NOT EXISTS domains(host TEXT PRIMARY KEY, epoch INTEGER DEFAULT 0, owner TEXT,
 next_at REAL DEFAULT 0, stopped_until REAL DEFAULT 0, failures INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, reservation_id TEXT, generation INTEGER,
 host TEXT, state TEXT, started REAL);
CREATE UNIQUE INDEX IF NOT EXISTS one_connection ON requests(host) WHERE state='active';
CREATE TABLE IF NOT EXISTS processes(reservation_id TEXT PRIMARY KEY, pid INTEGER, start TEXT,
 instance TEXT, state TEXT);
CREATE TABLE IF NOT EXISTS observations(seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT,
 reservation_id TEXT UNIQUE, data TEXT, observed REAL);
CREATE TABLE IF NOT EXISTS history(seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT, stage TEXT,
 reason TEXT, at REAL);
CREATE TABLE IF NOT EXISTS checkpoints(seed TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS conflict_resolutions(id TEXT PRIMARY KEY, evidence_id TEXT, reason TEXT, at REAL);
CREATE TABLE IF NOT EXISTS archives(reservation_id TEXT PRIMARY KEY, permission TEXT, content TEXT);
CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT, operation TEXT, reason TEXT, at REAL);
CREATE TABLE IF NOT EXISTS grants(origin TEXT PRIMARY KEY, extension_origin TEXT, enabled INTEGER);
CREATE TABLE IF NOT EXISTS consent_checks(id TEXT PRIMARY KEY, extension_origin TEXT,
 origin TEXT, expires REAL, allowed INTEGER);
CREATE TABLE IF NOT EXISTS reviews(seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT,
 data TEXT, at REAL);
'''


BUSY_TIMEOUT_S = 30
INIT_ATTEMPTS = 6


class Store:
    def __init__(self, path=None, readonly=False):
        self.path = external_path(path) if path else state_path() / 'collector.sqlite3'
        if not readonly:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path.as_uri() + '?mode=ro' if readonly else str(self.path),
                                  uri=readonly, timeout=BUSY_TIMEOUT_S, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute(f'PRAGMA busy_timeout={int(BUSY_TIMEOUT_S * 1000)}')
        self.db.execute('PRAGMA foreign_keys=ON')
        if not readonly:
            os.chmod(self.path, 0o600)
            self._init_with_retry()

    def _init_with_retry(self, attempts=None, base_delay=0.5):
        # Startup can race a worker holding a write lock; journal_mode switches and DDL may
        # raise 'database is locked' despite busy_timeout, so retry with exponential backoff.
        attempts = attempts or INIT_ATTEMPTS
        for i in range(attempts):
            try:
                self.db.execute('PRAGMA journal_mode=WAL')
                self.db.executescript(SQL)
                return
            except sqlite3.OperationalError as e:
                if 'locked' not in str(e) and 'busy' not in str(e) or i == attempts - 1:
                    raise
                time.sleep(min(base_delay * (2 ** i), 8))

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield self.db
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def close(self):
        self.db.close()
