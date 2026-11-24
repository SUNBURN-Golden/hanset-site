"""KST collector schedules shared by the watchdog and crontab installer."""
from dataclasses import dataclass
from datetime import timedelta

from ..util.timeutil import KST

GRACE = timedelta(minutes=10)
CHECKOUT = '/workspace/hanset-site'
DATA = '/workspace/hanset-data'
ODD_HOURS = tuple(range(1, 24, 2))


@dataclass(frozen=True)
class Job:
    hours: tuple
    minute: int
    script: str
    args: tuple = ()
    tagged: bool = False
    timeout: tuple = ()

    def latest(self, now):
        now = now.astimezone(KST)
        for hour in reversed(self.hours):
            slot = now.replace(hour=hour, minute=self.minute, second=0, microsecond=0)
            if slot <= now:
                return slot
        return slot.replace(hour=self.hours[-1]) - timedelta(days=1)


COLLECT_TIMEOUT = ('--signal=TERM', '--kill-after=10s', '25m')
SHARED_JOBS = ('wide_collect', 'daily_collect', 'active')
WATCHDOG_MINUTES = 15

JOBS = {
    'seed_discovery': Job(ODD_HOURS, 15, 'run_seed_discovery_cron.sh', tagged=True, timeout=('--signal=TERM', '--kill-after=10s', '110m')),
    'wide_collect': Job(ODD_HOURS, 15, 'run_collect_cron.sh', ('wide',), tagged=True, timeout=COLLECT_TIMEOUT),
    'daily_collect': Job((4,), 15, 'run_collect_cron.sh', timeout=COLLECT_TIMEOUT),
    'active': Job(tuple(range(24)), 35, 'run_active_cron.sh'),
    'twin': Job(tuple(range(24)), 50, 'run_twin_cron.sh', timeout=('20m',)),
}
MARKER = b'# hanset-collx'


def owned_entries():
    env = (f'HANSET_DATA_DIR={DATA} HANSET_ACTIVE_DIR={DATA}/active '
           f'HANSET_IG_DIR={DATA}/ig HANSET_TWIN_DIR={DATA}/twin')
    entries = {}
    for name, job in JOBS.items():
        if job.tagged:
            publish = ' HANSET_AUTO_PUBLISH=1' if name == 'wide_collect' else ''
            command = ' '.join((f'{CHECKOUT}/scripts/{job.script}', *job.args))
            step = job.hours[1] - job.hours[0]
            schedule = f'{job.minute} {job.hours[0]}-{job.hours[-1]}/{step} * * *'
            entries[name] = f'{schedule} {env}{publish} {command}'.encode()
    entries['watchdog'] = f'*/{WATCHDOG_MINUTES} * * * * {env} {CHECKOUT}/scripts/collect_watchdog.sh'.encode()
    return {name: line + b' ' + MARKER + b'\n' for name, line in entries.items()}
