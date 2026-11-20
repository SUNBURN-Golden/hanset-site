"""SQLite schema and atomic persisted daily budgets."""
import hashlib
import json
import sqlite3
from pathlib import Path
from .util.timeutil import kst_day

def deterministic_id(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()).hexdigest()

def migrate(conn: sqlite3.Connection) -> None:
    conn.execute('CREATE TABLE IF NOT EXISTS schema_version(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
    for path in sorted((Path(__file__).parent / 'migrations').glob('*.sql')):
        # Check the marker again under the write lock. Two independent
        # entry points can open the same store during an upgrade.
        if conn.execute('SELECT 1 FROM schema_version WHERE version=?', (path.name,)).fetchone():
            continue
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM schema_version WHERE version=?', (path.name,)).fetchone():
                continue
            script = path.read_text()
            start = 0
            for index, char in enumerate(script):
                if char == ';' and sqlite3.complete_statement(script[start:index + 1]):
                    conn.execute(script[start:index + 1])
                    start = index + 1
            if script[start:].strip():
                conn.execute(script[start:])
            conn.execute('INSERT INTO schema_version(version) VALUES (?)', (path.name,))

def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ':memory:':
        from .config import REPO_ROOT
        resolved = Path(path).resolve()
        if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
            raise ValueError('Database must be outside checkout')
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    migrate(conn)
    return conn

def consume_budget(conn, kind: str, cap: int, clock=None) -> bool:
    if kind not in ('candidates','bodies','aux'):
        raise ValueError('Unknown budget kind')
    day = kst_day(clock)
    if cap <= 0:
        return False
    with conn:
        conn.execute('INSERT INTO budgets_daily(day,kind,used) VALUES (?,?,1) ON CONFLICT(day,kind) DO UPDATE SET used=used+1 WHERE used < ?', (day,kind,cap))
        return bool(conn.execute('SELECT changes()').fetchone()[0])
