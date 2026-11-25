"""Exercise the installer against a byte-preserving fake crontab."""
import os
import subprocess
import sys

import pytest

from hanset_active.collect import cron_install
from hanset_active.collect.private_io import json_bytes, replace
from hanset_active.collect.schedules import owned_entries


@pytest.fixture
def crontab(tmp_path):
    state = tmp_path / 'table'
    writes = tmp_path / 'writes'
    fake = tmp_path / 'crontab'
    fake.write_text(f'#!{sys.executable}\n'
                    'import os, sys\nfrom pathlib import Path\n'
                    "state = Path(os.environ['FAKE_CRONTAB'])\n"
                    "if sys.argv[1:] == ['-l']:\n"
                    "    if os.environ.get('FAKE_READ_FAILURE'):\n"
                    "        sys.stderr.write('private read failure\\n')\n"
                    "        sys.exit(1)\n"
                    "    if not state.exists():\n"
                    "        sys.stderr.write('no crontab for test-user\\n')\n"
                    "        sys.exit(1)\n"
                    '    sys.stdout.buffer.write(state.read_bytes())\n'
                    "elif sys.argv[1:] == ['-']:\n"
                    '    state.write_bytes(sys.stdin.buffer.read())\n'
                    "    with Path(os.environ['FAKE_WRITES']).open('ab') as log: log.write(b'+')\n"
                    'else:\n'
                    '    sys.exit(2)\n')
    fake.chmod(0o755)
    root = tmp_path / 'runtime'
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ['PATH'],
               HANSET_CRON_TIMEZONE='Asia/Seoul',
               FAKE_CRONTAB=str(state), FAKE_WRITES=str(writes))

    def invoke(*args, **extra):
        script = ('import sys; from hanset_active.collect.cron_install import main; '
                  'sys.exit(main(sys.argv[2:], data_root=sys.argv[1]))')
        return subprocess.run([sys.executable, '-c', script, str(root), *args],
                              env={**env, **extra}, capture_output=True, timeout=10)

    return root, state, writes, invoke


def ready(root):
    replace(root / 'publication/v1/readiness.json', json_bytes({
        'schema_version': 1, 'consumer_version': 'fake-v1',
        'non_publishing_check': 'passed', 'confirmed_at': '2026-11-20T00:00:00+09:00',
    }))


def test_install_replaces_in_place_and_uninstall_preserves_near_matches(crontab):
    root, state, writes, invoke = crontab
    ready(root)
    first = (b'CRON_TZ = "Asia/Seoul"\r\n# note # hanset-collx\n'
             b'CONFIG = value # hanset-collx\n15 4 * * * /existing/collect\n'
             b'35 * * * * /existing/active\n50 * * * * /existing/twin\n5 * * * * /existing/crawler\n')
    middle = (b'5 * * * * /existing/task# hanset-collx\n'
              b'5 * * * * /existing/task "# hanset-collx\n'
              b'5 * * * * /existing/task \\# hanset-collx\n'
              b'99 * * * * /existing/task # hanset-collx\n'
              b'* * * * * # hanset-collx\n')
    tail = b'@weekly /existing/unmanaged\n'
    seed = b'15 1 * * * /workspace/hanset-site/scripts/run_seed_discovery_cron.sh'
    stale_alarm = b'@hourly /old/task fixture-private-value # hanset-collx\n'
    owned_seed = seed + b' # hanset-collx\n'
    old = first + owned_seed + middle + owned_seed + stale_alarm + tail
    state.write_bytes(old)
    preview = invoke('--dry-run')
    assert preview.returncode == 0 and preview.stdout.count(b'+15 1-23/2 * * * ') == 2
    assert b'+*/15 * * * * ' in preview.stdout and b'-[redacted] # hanset-collx' in preview.stdout
    assert b'fixture-private-value' not in preview.stdout + preview.stderr
    assert state.read_bytes() == old and not writes.exists()
    assert invoke().returncode == 0
    entries = owned_entries()
    expected = first + entries['seed_discovery'] + middle + tail + entries['wide_collect'] + entries['watchdog']
    assert state.read_bytes() == expected
    assert invoke().returncode == 0 and writes.read_bytes() == b'+'
    preview = invoke('--uninstall', '--dry-run')
    assert preview.returncode == 0 and preview.stdout.count(b'-[redacted] # hanset-collx') == 3
    assert state.read_bytes() == expected and writes.read_bytes() == b'+'
    assert invoke('--uninstall').returncode == 0
    assert state.read_bytes() == first + middle + tail and writes.read_bytes() == b'++'


def test_absent_read_failure_readiness_and_timezone(crontab):
    root, state, writes, invoke = crontab
    preview = invoke('--dry-run')
    assert preview.returncode != 0 and b'publication_readiness_required' in preview.stderr
    assert preview.stdout.count(b'+15 1-23/2 * * * ') == 2
    assert invoke().returncode != 0 and not state.exists() and not writes.exists()
    ready(root)
    denied = invoke(FAKE_READ_FAILURE='1')
    assert denied.returncode != 0 and denied.stderr == b'crontab_read_failed\n'
    assert not writes.exists()
    state.write_bytes(b'CRON_TZ = UTC\n')
    assert invoke().returncode != 0 and state.read_bytes() == b'CRON_TZ = UTC\n'
    assert not writes.exists()
    state.unlink()
    assert invoke('--uninstall', '--dry-run').returncode == 0 and not writes.exists()
    assert invoke().returncode == 0 and len(state.read_bytes().splitlines()) == 3
    assert cron_install.timezone_conflict(state.read_bytes(), 'UTC') == 'seed_discovery'


def test_daily_publish_opt_in_is_reversible_without_duplicate_job(crontab):
    root, state, _, invoke = crontab
    ready(root)
    daily = b'15 04 * * *  /workspace/hanset-site/scripts/run_collect_cron.sh web\r\n'
    state.write_bytes(daily)
    assert invoke().returncode == 0
    assert state.read_bytes().startswith(daily)
    preview = invoke('--daily-publish', '--dry-run')
    assert preview.returncode == 0 and b'HANSET_AUTO_PUBLISH=1' in preview.stdout
    assert invoke('--daily-publish').returncode == 0
    lines = state.read_bytes().splitlines()
    assert sum(line.startswith(b'15 04 ') for line in lines) == 1
    assert b'HANSET_AUTO_PUBLISH=1 ' in lines[0]
    assert invoke('--uninstall').returncode == 0
    assert state.read_bytes() == daily
