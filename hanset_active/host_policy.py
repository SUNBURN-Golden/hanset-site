"""Durable timing and refusal state for the public web fetcher."""
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from .db import deterministic_id
from .locks import hostname
from .network_store import open_audit

BACKOFF_STATUSES = (429, 503)
ROBOTS_TTL = timedelta(hours=24)
BLOCK_WINDOW = timedelta(days=14)


def origin(url):
    parts = urlsplit(url)
    host = hostname(url)
    authority = f'[{host}]' if ':' in host else host
    if parts.port is not None and parts.port != {'http': 80, 'https': 443}.get(parts.scheme):
        authority += f':{parts.port}'
    return f'{parts.scheme}://{authority}'


def resource_id(url):
    parts = urlsplit(url)
    return deterministic_id(origin(url), parts.path or '/', parts.query)


def retry_after(raw, now):
    try:
        try:
            at = now + timedelta(seconds=int(raw))
        except ValueError:
            at = parsedate_to_datetime(raw)
        if at.utcoffset() is not None and at > now:
            return at.astimezone(now.tzinfo)
    except (TypeError, ValueError, OverflowError):
        pass
    return None


class HostPolicy:
    """Callers serialize each host and guard use of the shared SQLite connection."""

    def __init__(self, directory, clock, min_interval):
        self.db = open_audit(directory)
        self.clock = clock
        self.min_interval = max(5, min_interval)

    def close(self):
        self.db.close()

    def stopped(self, url):
        row = self.db.execute('SELECT retry_at FROM host_backoff WHERE host=?', (hostname(url),)).fetchone()
        if row and datetime.fromisoformat(row[0]) > self.clock.now():
            return 'host_backoff', row[0]
        row = self.db.execute('SELECT hold_until FROM resource_blocks WHERE url_hash=?', (resource_id(url),)).fetchone()
        if row and row[0] and datetime.fromisoformat(row[0]) > self.clock.now():
            return 'resource_hold', row[0]
        return '', None

    def block(self, url, raw_retry):
        now = self.clock.now()
        at = retry_after(raw_retry, now)
        self._strike(url, now, at=at)
        return at.isoformat() if at else None

    def refuse(self, url):
        self._strike(url, self.clock.now(), immediate=True)

    def _strike(self, url, now, *, at=None, immediate=False):
        key = resource_id(url)
        with self.db:
            row = self.db.execute('SELECT struck_at,hold_until FROM resource_blocks WHERE url_hash=?', (key,)).fetchone()
            hold = datetime.fromisoformat(row['hold_until']) if row and row['hold_until'] else None
            if immediate or (row and datetime.fromisoformat(row['struck_at']) + BLOCK_WINDOW > now):
                hold = max(hold or now, now + BLOCK_WINDOW)
            self.db.execute('INSERT OR REPLACE INTO resource_blocks VALUES (?,?,?)',
                            (key, now.isoformat(), hold.isoformat() if hold else None))
            if at:
                self.db.execute('''INSERT INTO host_backoff VALUES (?,?)
                    ON CONFLICT(host) DO UPDATE SET retry_at=excluded.retry_at
                    WHERE julianday(excluded.retry_at)>julianday(host_backoff.retry_at)''',
                                (hostname(url), at.isoformat()))

    def delay(self, url, crawl_delay):
        row = self.db.execute('SELECT requested_at,crawl_delay FROM host_timing WHERE host=?', (hostname(url),)).fetchone()
        if not row or not row[0]:
            return 0
        age = (self.clock.now() - datetime.fromisoformat(row[0])).total_seconds()
        return max(0, max(self.min_interval, crawl_delay, row[1]) - age)

    def dispatch(self, url):
        with self.db:
            self.db.execute('''INSERT INTO host_timing(host,requested_at) VALUES (?,?)
                ON CONFLICT(host) DO UPDATE SET requested_at=excluded.requested_at''',
                            (hostname(url), self.clock.now().isoformat()))

    def cached_robots(self, url):
        row = self.db.execute('SELECT body,expires_at FROM news_robots WHERE origin=?', (origin(url),)).fetchone()
        if row and datetime.fromisoformat(row['expires_at']) > self.clock.now():
            return row['body']
        return None

    def reserve_robots(self, url):
        host = hostname(url)
        now = self.clock.now()
        row = self.db.execute('SELECT robots_attempt FROM host_timing WHERE host=?', (host,)).fetchone()
        if row and row[0] and datetime.fromisoformat(row[0]) + ROBOTS_TTL > now:
            return None
        attempt = now.isoformat()
        with self.db:
            self.db.execute('''INSERT INTO host_timing(host,robots_attempt) VALUES (?,?)
                ON CONFLICT(host) DO UPDATE SET robots_attempt=excluded.robots_attempt''', (host, attempt))
        return attempt

    def release_robots(self, url, attempt):
        with self.db:
            self.db.execute('UPDATE host_timing SET robots_attempt=NULL WHERE host=? AND robots_attempt=?',
                            (hostname(url), attempt))

    def store_robots(self, url, body, crawl_delay, attempt):
        with self.db:
            updated = self.db.execute('UPDATE host_timing SET crawl_delay=? WHERE host=? AND robots_attempt=?',
                                      (crawl_delay, hostname(url), attempt))
            if updated.rowcount:
                expiry = datetime.fromisoformat(attempt) + ROBOTS_TTL
                self.db.execute('INSERT OR REPLACE INTO news_robots VALUES (?,?,?)',
                                (origin(url), body, expiry.isoformat()))
