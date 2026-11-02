import sqlite3
import threading
import time

from crawler import store as store_mod
from crawler.store import Store


def test_store_init_survives_held_write_lock(tmp_path):
    path = tmp_path / 'c.sqlite3'
    sqlite3.connect(str(path)).execute('CREATE TABLE x(a)')  # rollback-journal DB, like a legacy file
    holder = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    holder.execute('BEGIN EXCLUSIVE')
    threading.Timer(1.5, lambda: holder.execute('COMMIT')).start()
    t0 = time.time()
    s = Store(path)
    assert time.time() - t0 >= 1.0
    assert s.db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert s.db.execute('PRAGMA busy_timeout').fetchone()[0] == store_mod.BUSY_TIMEOUT_S * 1000
    s.close(); holder.close()


def test_init_retry_backs_off_then_succeeds(tmp_path, monkeypatch):
    s = Store(tmp_path / 'r.sqlite3')
    calls = {'n': 0}
    real = s.db

    class Flaky:
        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, sql, *a):
            if sql.startswith('PRAGMA journal_mode') and calls['n'] < 2:
                calls['n'] += 1
                raise sqlite3.OperationalError('database is locked')
            return real.execute(sql, *a)

    s.db = Flaky()
    monkeypatch.setattr(store_mod.time, 'sleep', lambda _: None)
    s._init_with_retry()
    assert calls['n'] == 2
    s.db = real; s.close()
