"""Twin DB (separate from the active-bot store and from the code checkout)."""
import json
import os
import sqlite3
from pathlib import Path
from hanset_active.config import REPO_ROOT
from hanset_active.db import deterministic_id
from hanset_active.util.timeutil import kst_now

DEFAULT_DIR = Path(os.environ.get('HANSET_TWIN_DIR', '/workspace/hanset-data/twin'))
DEFAULT_ACTIVE = Path(os.environ.get('HANSET_ACTIVE_DIR', '/workspace/hanset-data/active')) / 'active.sqlite3'

def connect(path):
    if str(path) != ':memory:':
        resolved = Path(path).resolve()
        if REPO_ROOT in resolved.parents:
            raise ValueError('Twin database must be outside checkout')
        resolved.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('CREATE TABLE IF NOT EXISTS schema_version(version TEXT PRIMARY KEY, applied_at TEXT)')
    for sql in sorted((Path(__file__).parent / 'migrations').glob('*.sql')):
        if not db.execute('SELECT 1 FROM schema_version WHERE version=?', (sql.name,)).fetchone():
            db.executescript('BEGIN IMMEDIATE;\n' + sql.read_text() +
                f"\nINSERT INTO schema_version VALUES ('{sql.name}','{kst_now().isoformat()}');\nCOMMIT;")
    return db

def open_active_readonly(path):
    """Active-bot store is opened read-only; the twin never writes to it."""
    db = sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db

def vhash(value):
    return deterministic_id('v', value)

def dumps(value):
    return None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)
