"""Durable, fail-closed Tavily reservations, serialized through dispatch on Linux."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import threading

from ..config import REPO_ROOT
from ..util.timeutil import KST, kst_day, kst_now

DAILY_LIMIT = 30
_STATE_LIMIT = 1024 * 1024
_PROCESS_LOCK = threading.Lock()
REASONS = frozenset({
    'ok', 'tavily_budget_exhausted', 'tavily_cooldown', 'tavily_clock_changed',
    'tavily_http_error', 'tavily_rate_limited', 'tavily_timeout',
    'tavily_transport_error', 'tavily_invalid_response', 'tavily_response_too_large',
})


@dataclass(frozen=True)
class BudgetSnapshot:
    day: str
    used: int
    daily_limit: int = DAILY_LIMIT

    @property
    def remaining(self):
        return max(0, DAILY_LIMIT - self.used)


@dataclass(frozen=True)
class BudgetAvailability:
    reason: str
    snapshot: BudgetSnapshot | None = None
    retry_at: str | None = None


class BudgetUnavailable(Exception):
    def __init__(self):
        super().__init__('tavily_budget_unavailable')


def _private_open(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise BudgetUnavailable()
        return fd
    except BaseException:
        os.close(fd)
        raise


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BudgetUnavailable()
        result[key] = value
    return result


def _read(path):
    try:
        fd = _private_open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return {'schema_version': 1, 'budgets': {'tavily': {
            'daily_limit': DAILY_LIMIT, 'days': {}, 'last_reason': 'ok'}}}
    with os.fdopen(fd, 'rb') as stream:
        data = stream.read(_STATE_LIMIT + 1)
    if len(data) > _STATE_LIMIT:
        raise BudgetUnavailable()
    state = json.loads(data, object_pairs_hook=_unique_object)
    # Never preserve arbitrary state fields (including accidentally stored secrets).
    if set(state) != {'schema_version', 'budgets'} or type(state['schema_version']) is not int or state['schema_version'] != 1:
        raise BudgetUnavailable()
    if set(state['budgets']) != {'tavily'}:
        raise BudgetUnavailable()
    budget = state['budgets']['tavily']
    required = {'daily_limit', 'days', 'last_reason'}
    if set(budget) not in (required, required | {'cooldown_until'}):
        raise BudgetUnavailable()
    if type(budget['daily_limit']) is not int or budget['daily_limit'] != DAILY_LIMIT:
        raise BudgetUnavailable()
    if budget['last_reason'] not in REASONS or not isinstance(budget['days'], dict):
        raise BudgetUnavailable()
    for day, counter in budget['days'].items():
        if date.fromisoformat(day).isoformat() != day or set(counter) != {'used'}:
            raise BudgetUnavailable()
        if type(counter['used']) is not int or not 0 <= counter['used'] <= DAILY_LIMIT:
            raise BudgetUnavailable()
    cooldown = budget.get('cooldown_until', 0)
    if type(cooldown) not in (int, float) or not math.isfinite(cooldown) or cooldown < 0:
        raise BudgetUnavailable()
    return state


def _write(path, state):
    """Commit before sending; even directory-fsync failure prevents dispatch."""
    fd, temp = tempfile.mkstemp(prefix='.tavily-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(state, stream, sort_keys=True, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Reservation:
    def __init__(self, path, state, clock):
        self.path, self.state, self.clock = path, state, clock
        self.snapshot = None
        self.reason = 'ok'

    def availability(self):
        """Inspect under the dispatch lock without reserving or writing state."""
        now = kst_now(self.clock)
        budget = self.state['budgets']['tavily']
        snapshot = BudgetSnapshot(now.date().isoformat(), budget['days'].get(now.date().isoformat(), {}).get('used', 0))
        if not snapshot.remaining:
            retry = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            return BudgetAvailability('tavily_budget_exhausted', snapshot, retry.isoformat())
        cooldown = budget.get('cooldown_until', 0)
        if cooldown > now.timestamp():
            return BudgetAvailability('tavily_cooldown', snapshot, datetime.fromtimestamp(cooldown, KST).isoformat())
        return BudgetAvailability('ok', snapshot)

    def reserve(self):
        day = kst_day(self.clock)
        budget = self.state['budgets']['tavily']
        counter = budget['days'].setdefault(day, {'used': 0})
        self.snapshot = BudgetSnapshot(day, counter['used'])
        if counter['used'] >= DAILY_LIMIT:
            self.reason = 'tavily_budget_exhausted'
        elif budget.get('cooldown_until', 0) > kst_now(self.clock).timestamp():
            self.reason = 'tavily_cooldown'
        else:
            counter['used'] += 1
            self.snapshot = BudgetSnapshot(day, counter['used'])
        budget['last_reason'] = self.reason
        _write(self.path, self.state)
        return self.reason == 'ok'

    def ready_to_dispatch(self):
        # A rollover during fsync must reserve against the new day's allowance too.
        # A rapidly changing clock skips conservatively instead of looping forever.
        if self.snapshot.day != kst_day(self.clock):
            if not self.reserve():
                return False
            if self.snapshot.day != kst_day(self.clock):
                self.finish('tavily_clock_changed')
                return False
        return True

    def finish(self, reason, cooldown_seconds=0):
        if reason not in REASONS:
            raise BudgetUnavailable()
        self.reason = reason
        budget = self.state['budgets']['tavily']
        budget['last_reason'] = reason
        if cooldown_seconds:
            budget['cooldown_until'] = kst_now(self.clock).timestamp() + min(3600, max(1, cooldown_seconds))
        _write(self.path, self.state)


def _directory():
    configured = os.environ.get('HANSET_IG_DIR')
    if not configured:
        raise BudgetUnavailable()
    directory = Path(configured).resolve()
    if directory == REPO_ROOT or REPO_ROOT in directory.parents:
        raise BudgetUnavailable()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not directory.is_dir():
        raise BudgetUnavailable()
    return directory


@contextmanager
def locked_budget(*, clock=None):
    """The caller must hold this context through the single outbound request."""
    # Do not hold this lock across fork; daemon callers should fork before calling.
    with _PROCESS_LOCK:
        with_error = False
        try:
            directory = _directory()
            fd = _private_open(directory / 'tavily_usage.lock', os.O_RDWR | os.O_CREAT | os.O_NONBLOCK)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                reservation = Reservation(directory / 'tavily_usage.json', _read(directory / 'tavily_usage.json'), clock)
            except BaseException:
                os.close(fd)
                raise
        except Exception:
            with_error = True
        # Raise outside except so even formatted exceptions have no raw context.
        if with_error:
            raise BudgetUnavailable()
        try:
            yield reservation
        finally:
            os.close(fd)


def availability(*, clock=None):
    """Non-reserving advisory check; reservation at dispatch stays authoritative."""
    try:
        with locked_budget(clock=clock) as reservation:
            return reservation.availability()
    except Exception:
        return BudgetAvailability('tavily_budget_unavailable')
