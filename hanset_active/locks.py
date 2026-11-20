"""Process overlap protection and renewable SQLite run leases."""
from contextlib import contextmanager
import fcntl
import os
import re
from datetime import timedelta
from pathlib import Path
from .util.timeutil import Clock, kst_now

class AlreadyRunning(RuntimeError):
    pass

class RunLock:
    def __init__(self, db, data_dir, run_id, *, clock=None, lease_seconds=120,
                 filename='run.lock', lease_key='collector'):
        self.db, self.path, self.run_id = db, Path(data_dir)/filename, run_id
        self.clock = clock or Clock()
        self.lease_seconds = lease_seconds
        self.lease_key = lease_key
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a')
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            self.file = None
            raise AlreadyRunning('already_running') from None
        try:
            # Owning flock proves any previous lease belongs to a terminated
            # process, even if its timestamp has not expired yet.
            self.heartbeat()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def heartbeat(self):
        now = kst_now(self.clock)
        until = (now + timedelta(seconds=self.lease_seconds)).isoformat()
        with self.db:
            self.db.execute("INSERT INTO run_lease VALUES (?,?,?,?) ON CONFLICT(lease_key) DO UPDATE SET run_id=excluded.run_id,heartbeat=excluded.heartbeat,lease_until=excluded.lease_until", (self.lease_key, self.run_id, now.isoformat(), until))
        return until

    def __exit__(self, *args):
        if self.file is not None:
            try:
                with self.db:
                    self.db.execute("DELETE FROM run_lease WHERE lease_key=? AND run_id=?", (self.lease_key, self.run_id))
            finally:
                fcntl.flock(self.file, fcntl.LOCK_UN)
                self.file.close()
                self.file = None


@contextmanager
def host_lock(data_dir, domain):
    """Hold one domain per process/thread, releasing it before a cross-domain hop."""
    if not re.fullmatch(r'[a-z0-9.:-]+', domain):
        raise ValueError('invalid_host')
    directory = Path(data_dir) / 'host-locks'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory / (domain + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)
