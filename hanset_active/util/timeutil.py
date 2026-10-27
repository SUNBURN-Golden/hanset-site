"""Timezone-aware clocks; sleepers are injectable for deterministic tests."""
import time
from datetime import datetime, timezone, timedelta
KST = timezone(timedelta(hours=9))

class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)
    def monotonic(self) -> float:
        return time.monotonic()
    def sleep(self, seconds: float) -> None:
        time.sleep(max(0, seconds))

class FakeClock(Clock):
    def __init__(self, start: datetime | None = None):
        self.start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.elapsed = 0.0
    def now(self):
        return self.start + timedelta(seconds=self.elapsed)
    def monotonic(self):
        return self.elapsed
    def sleep(self, seconds):
        self.elapsed += max(0, seconds)

def kst_now(clock: Clock | None = None) -> datetime:
    return (clock or Clock()).now().astimezone(KST)

def kst_day(clock: Clock | None = None) -> str:
    return kst_now(clock).date().isoformat()
