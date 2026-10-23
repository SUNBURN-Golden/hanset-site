"""Expiry revokes authority; it does not release domain ownership."""
import os
import signal
import time
from pathlib import Path
from .config import Rejected


def current(db, reservation, now, running=False):
    row = db.execute('SELECT * FROM reservations WHERE id=?', (reservation['id'],)).fetchone()
    latest = db.execute('SELECT MAX(generation) FROM reservations WHERE record_id=?', (row['record_id'],)).fetchone()[0] if row else None
    if not row or row['generation'] != reservation['generation'] or row['generation'] != latest:
        raise Rejected('stale_generation')
    if row['state'] not in (('running',) if running else ('reserved', 'running')) or row['expires'] <= now:
        raise Rejected('lease_expired_or_revoked')
    domain = db.execute('SELECT * FROM domains WHERE host=?', (row['domain'],)).fetchone()
    if domain['owner'] != row['id'] or domain['epoch'] != row['domain_epoch']:
        raise Rejected('stale_generation')
    return row


def process_start(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def process_alive(pid, start):
    actual = process_start(pid)
    if actual is None:
        # Lack of permission is not proof of exit.
        return Path(f'/proc/{pid}').exists()
    if actual != start:
        return False
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except OSError:
        return Path(f'/proc/{pid}').exists()


class Supervisor:
    def __init__(self, worker):
        self.worker = worker
        self.runners = {}
        self.gates = {}

    def expire(self):
        w = self.worker
        with w.store.transaction() as db:
            db.execute("UPDATE reservations SET state='revoking' WHERE state IN ('running','reserved') AND expires<=?", (w.clock(),))
        rows = w.store.db.execute("SELECT * FROM reservations WHERE state='revoking'").fetchall()
        for row in rows:
            self.drain(dict(row))

    def drain(self, reservation):
        w = self.worker
        rid = reservation['id']
        gate = self.gates.get(rid)
        if gate:
            gate.cancel()
        runner = self.runners.get(rid)
        if runner:
            runner.stop()
        proc = w.store.db.execute('SELECT * FROM processes WHERE reservation_id=?', (rid,)).fetchone()
        if proc and process_alive(proc['pid'], proc['start']):
            # Only exact recorded process identity is signalled, never a recycled PID.
            if not proc['start'] or process_start(proc['pid']) != proc['start']:
                return False
            os.killpg(proc['pid'], signal.SIGKILL)
            return False  # later supervisor pass confirms exit
        active = w.store.db.execute("SELECT count(*) FROM requests WHERE reservation_id=? AND state='active'", (rid,)).fetchone()[0]
        if active:
            return False
        if runner and not runner.exited():
            return False
        with w.store.transaction() as db:
            db.execute("UPDATE reservations SET state='drained' WHERE id=? AND state IN ('revoking','completed')", (rid,))
            state = db.execute('SELECT state FROM reservations WHERE id=?', (rid,)).fetchone()
            if not state or state[0] != 'drained':
                return False
            db.execute('UPDATE domains SET owner=NULL WHERE owner=?', (rid,))
            db.execute("UPDATE processes SET state='exited' WHERE reservation_id=?", (rid,))
        return True

    def recover(self):
        # Unknown previous owner is held until its OS identity is known dead.
        w = self.worker
        # A loopback server and a --once worker may coexist. A live owner's
        # generation is not a crash merely because this is another instance.
        with w.store.transaction() as db:
            for row in db.execute("SELECT * FROM reservations WHERE worker!=? AND state IN ('running','reserved','completed')", (w.instance,)).fetchall():
                owner = row['worker'].split(':')
                if len(owner) == 3 and not process_alive(int(owner[0]), owner[1]):
                    db.execute("UPDATE reservations SET state='revoking' WHERE id=?", (row['id'],))
        for row in w.store.db.execute("SELECT * FROM reservations WHERE state='revoking'").fetchall():
            # Requests from a different worker cannot be declared closed on elapsed time alone.
            owner = row['worker'].split(':')
            if len(owner) == 3 and not process_alive(int(owner[0]), owner[1]):
                w.store.db.execute("UPDATE requests SET state='closed' WHERE reservation_id=?", (row['id'],))
            self.drain(dict(row))
