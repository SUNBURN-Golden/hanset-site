"""KST accounting counts authorizations, never just successful saves."""
import datetime as dt
from zoneinfo import ZoneInfo
from .config import Rejected

KST = ZoneInfo('Asia/Seoul')


def day_and_expiry(now, ttl):
    local = dt.datetime.fromtimestamp(now, KST)
    midnight = dt.datetime.combine(local.date() + dt.timedelta(days=1), dt.time(), KST).timestamp()
    if midnight - now < 15:
        raise Rejected('defer_until_next_day')
    return local.date().isoformat(), min(now + ttl, midnight)


def charge(db, cfg, day, brand, candidate=False, body=False, new_body=False):
    db.execute('INSERT OR IGNORE INTO counters(day,brand) VALUES (?,?)', (day, brand))
    row = db.execute('SELECT * FROM counters WHERE day=? AND brand=?', (day, brand)).fetchone()
    for key, limit, delta in (('candidates', 'daily_candidates', int(candidate)),
                              ('new_bodies', 'daily_new_bodies', int(new_body)),
                              ('attempts', 'daily_attempts', int(body))):
        if row[key] + delta > cfg[limit]:
            raise Rejected('quota_exhausted')
    db.execute('UPDATE counters SET candidates=candidates+?,new_bodies=new_bodies+?,attempts=attempts+? WHERE day=? AND brand=?',
               (int(candidate), int(new_body), int(body), day, brand))
