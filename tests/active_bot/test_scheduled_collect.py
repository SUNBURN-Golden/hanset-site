"""Scheduler, publication and accounting tests using private temporary state."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from datetime import datetime, timedelta
import hashlib
import json
import os
import signal
import subprocess
import sys
from threading import Event

import pytest
import requests

from hanset_active.collect import runtime, scheduled
from hanset_active.collect.change_feeds import ARTIFACTS, COMMON, canonical_bytes, sha
from hanset_active.collect.changes import DETECTOR_VERSION
from hanset_active.collect.config import CollectSettings
from hanset_active.collect.daily_metrics import KEYS, LEGACY_INVENTORY, LEGACY_KEYS
from hanset_active.collect.private_io import Busy, json_bytes, lock, replace
from hanset_active.collect.plan import assemble_targets
from hanset_active.collect.proxy_env import SELECTOR
from hanset_active.collect.schedules import GRACE, JOBS
from hanset_active.collect.targets import source_authorization
from hanset_active.collect.store import connect as claim_connect, digest, record
from hanset_active.config import REPO_ROOT, Settings
from hanset_active.crawl_policy import COLLECTION
from hanset_active.db import connect
from hanset_active.executor import Executor
from hanset_active.fetcher import Fetcher, USER_AGENT
from hanset_active.scheduler import enqueue
from hanset_active.util.timeutil import FakeClock, kst_now
from hanset_active.venues import upsert_venue
from hanset_twin.daemon import run_once
from hanset_twin.stages import change_detect, extract, incremental_fetch
from hanset_twin.store import connect as twin_connect
from tests.active_bot.test_collect_promotion import approval
from tests.active_bot.test_collect_web import WebModel, _hours_answer, _page, _targets, enable_qwen
from tests.active_bot.test_seed_discovery import response

A = 'https://spa.example.test/hours'
B = 'https://box.example.test/hours'
C = 'https://hall.example.test/hours'
D = 'https://club.example.test/hours'


def at(value):
    return datetime.fromisoformat('2026-11-' + value + ':00+09:00')


@pytest.fixture
def job(tmp_path, monkeypatch):
    for name in ('DEEPSEEK_API_KEY', 'TAVILY_API_KEY', 'HANSET_DEEPSEEK_MODEL', 'HANSET_COLLECT_PROMOTION_POLICY',
                 'HANSET_WEB_PROXY', 'HANSET_CATCHUP_SLOT'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('HANSET_QWEN_KEY_FILE', str(tmp_path / 'unused-qwen'))
    monkeypatch.setenv('HANSET_DATAGO_KEY_FILE', str(tmp_path / 'unused-datago'))
    settings = CollectSettings(tmp_path / 'ig', tmp_path / 'active', tmp_path / 'twin')
    return scheduled.Scheduled(tmp_path, settings, FakeClock(at('20T11:00')), testing=True)


class Wait:
    def __init__(self, finish):
        self.wait = finish


@pytest.fixture
def started_payloads(job, monkeypatch):
    calls = []

    def start(name, slot, _):
        calls.append((name, slot))
        return Wait(lambda: 0)

    monkeypatch.setattr(job, 'start_payload', start)
    return calls


def ready(job, monkeypatch):
    monkeypatch.setenv('HANSET_AUTO_PUBLISH', '1')
    replace(job.outbox.root / 'readiness.json', json_bytes({
        'schema_version': 1, 'consumer_version': 'test-v1', 'non_publishing_check': 'passed',
        'confirmed_at': at('19T11:00').isoformat(),
    }))


def checkpoint(job, slot, request=None):
    replace(job.checkpoint, json_bytes({'slot': slot.isoformat(), 'request': request,
                                       'completed_at': kst_now(job.clock).isoformat()}))


def fake_launch(job):
    return lambda name, slot: Wait(lambda: job.run_job(name, catchup=slot)[0])


@pytest.fixture
def proxy_launch(job, monkeypatch):
    for key, path in (('HANSET_DATA_DIR', job.root), ('HANSET_IG_DIR', job.settings.data_dir),
                      ('HANSET_ACTIVE_DIR', job.settings.active_dir), ('HANSET_TWIN_DIR', job.settings.twin_dir)):
        monkeypatch.setenv(key, str(path))
    for key in list(os.environ):
        if key.startswith('HANSET_WEB_PROXY'):
            monkeypatch.delenv(key)
    monkeypatch.setenv('HANSET_WEB_PROXY', 'fixture-original')
    monkeypatch.setenv('HANSET_WEB_PROXY_HEAL_CMD', '')
    monkeypatch.setenv('HANSET_AUTO_PUBLISH', '0')
    path = job.root / 'fixture private settings.json'
    monkeypatch.setenv(SELECTOR, str(path))
    return path


def test_file_proxy_reaches_scheduled_payload_and_watchdog_catchup(job, proxy_launch, monkeypatch):
    options = {'HANSET_WEB_PROXY': 'fixture-file-proxy', 'HANSET_WEB_PROXY_HEAL_CMD': 'fixture private helper'}
    replace(proxy_launch, json_bytes(options))
    ig = job.settings.data_dir
    conflicting = job.root / 'fixture wrong selector.json'
    env_file = ("HANSET_WEB_PROXY=fixture-conflicting-proxy\n"
                "HANSET_WEB_PROXY_HEAL_CMD='fixture conflicting helper'\n"
                "HANSET_WEB_PROXY_EXIT_IP=fixture-extra-setting\n"
                f"{SELECTOR}='{conflicting}'\n")
    replace(ig / 'deepseek.env', env_file.encode())
    probe = job.root / 'payloads.jsonl'
    launcher = job.root / 'bin/python3'
    launcher.parent.mkdir()
    launcher.write_text(f'#!{sys.executable}\n' + '''
import json
import os
from pathlib import Path
import sys

if sys.argv[1:3] != ['-m', 'hanset_active.collect.scheduled'] or sys.argv[4:5] != ['--payload']:
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
name, slot = sys.argv[3], sys.argv[5]
row = dict(job=name, catchup=os.environ.get('HANSET_CATCHUP_SLOT'), proxy={
    key: value for key, value in os.environ.items()
    if key.startswith('HANSET_WEB_PROXY') or key == 'HANSET_COLLX_PROXY_ENV_FILE'
})
with Path(os.environ['TEST_PAYLOAD_PROBE']).open('a') as probe:
    print(json.dumps(row), file=probe)
if name == 'wide_collect':
    from hanset_active.collect.private_io import json_bytes, replace

    stamp = Path(os.environ['HANSET_DATA_DIR']) / 'stamps/wide_collect.completion.json'
    replace(stamp, json_bytes(dict(slot=slot, completed_at=slot, request=None)))
''')
    launcher.chmod(0o700)
    env = dict(os.environ, PATH=str(launcher.parent) + os.pathsep + os.environ['PATH'], TEST_PAYLOAD_PROBE=str(probe))

    def run(script):
        return subprocess.run([REPO_ROOT / 'scripts' / script], env=env, capture_output=True, text=True, timeout=60)

    now = kst_now()
    for name in JOBS.keys() - {'daily_collect'}:
        job.write_stamp(name, 'ok', now + timedelta(minutes=5))
    ordinary = run('run_collect_cron.sh')
    assert ordinary.returncode == 0
    assert [json.loads(line) for line in probe.read_text().splitlines()] == [
        {'job': 'daily_collect', 'catchup': None, 'proxy': options},
    ]

    updated = {**options, 'HANSET_WEB_PROXY': 'fixture-rotated-file-proxy'}
    replace(proxy_launch, json_bytes(updated))
    ready(job, monkeypatch)
    now = kst_now()
    daily = JOBS['daily_collect'].latest(now)
    # Use wide if the wall clock is near the daily slot and its grace period.
    target = 'daily_collect' if GRACE < now - daily < timedelta(hours=23, minutes=59) else 'wide_collect'
    for extension in ('ok', 'attempt'):
        (job.stamps / f'{target}.{extension}').unlink(missing_ok=True)
    for name in JOBS.keys() - {target}:
        job.write_stamp(name, 'ok', now + timedelta(minutes=5))
    watchdog = run('collect_watchdog.sh')
    assert watchdog.returncode == 0
    ordinary_payload, catchup_payload = [json.loads(line) for line in probe.read_text().splitlines()]
    assert ordinary_payload == {'job': 'daily_collect', 'catchup': None, 'proxy': options}
    assert catchup_payload == {'job': target, 'catchup': JOBS[target].latest(now).isoformat(), 'proxy': updated}

    logs = '\n'.join(path.read_text() for path in (ig / 'logs').glob('collect-*.log'))
    output = logs + ordinary.stdout + ordinary.stderr + watchdog.stdout + watchdog.stderr
    assert 'web_exit=0' in logs and 'proxy_env_file_invalid' not in output
    for private in (SELECTOR, str(proxy_launch), str(conflicting), *options.values(), *updated.values(),
                    'fixture-conflicting-proxy', 'fixture conflicting helper', 'fixture-extra-setting'):
        assert private not in output


@pytest.mark.parametrize('problem', ['missing', 'empty-path', 'relative', 'checkout', 'symlink', 'parent-symlink', 'directory',
                                    'fifo', 'permissions', 'owner', 'invalid-json', 'invalid-key', 'invalid-value', 'nul'])
def test_invalid_file_proxy_stops_before_dispatch(job, proxy_launch, problem, monkeypatch, capsys):
    safe = {'HANSET_WEB_PROXY': 'fixture-file-proxy'}
    if problem != 'missing':
        replace(proxy_launch, json_bytes(safe))
    if problem in ('empty-path', 'relative'):
        monkeypatch.setenv(SELECTOR, '' if problem == 'empty-path' else 'fixture-private-path')
    elif problem == 'checkout':
        monkeypatch.setenv(SELECTOR, str(REPO_ROOT / 'README.md'))
    elif problem == 'symlink':
        link = job.root / 'link'
        link.symlink_to(proxy_launch)
        monkeypatch.setenv(SELECTOR, str(link))
    elif problem == 'parent-symlink':
        link = job.root / 'link'
        link.symlink_to(job.root, target_is_directory=True)
        monkeypatch.setenv(SELECTOR, str(link / proxy_launch.name))
    elif problem == 'directory':
        monkeypatch.setenv(SELECTOR, str(job.root))
    elif problem == 'fifo':
        proxy_launch.unlink()
        os.mkfifo(proxy_launch, 0o600)
    elif problem == 'permissions':
        proxy_launch.chmod(0o644)
    elif problem == 'owner':
        uid = os.geteuid()
        monkeypatch.setattr(scheduled.os, 'geteuid', lambda: uid + 1)
    elif problem == 'invalid-json':
        replace(proxy_launch, b'fixture-invalid-json')
    elif problem == 'invalid-key':
        replace(proxy_launch, json_bytes({**safe, 'OTHER': 'fixture-invalid-key'}))
    elif problem == 'invalid-value':
        replace(proxy_launch, json_bytes({**safe, 'HANSET_WEB_PROXY_HEAL_CMD': ['fixture-invalid-value']}))
    elif problem == 'nul':
        replace(proxy_launch, json_bytes({**safe, 'HANSET_WEB_PROXY_HEAL_CMD': '\0fixture-invalid-value'}))

    monkeypatch.setattr(scheduled, 'Scheduled', lambda *_: pytest.fail('configuration reached dispatch'))
    for name in ('daily_collect', 'wide_collect', 'watchdog'):
        assert scheduled.main([name]) == 1
        assert capsys.readouterr() == ('', 'proxy_env_file_invalid\n')
        assert os.environ['HANSET_WEB_PROXY'] == 'fixture-original'


def test_seed_cron_races_watchdog_with_one_effective_payload(job, monkeypatch):
    job.clock.start = at('20T11:36')
    connect(job.metrics.active_path).close()
    for name in ('active', 'daily_collect', 'twin'):
        job.write_stamp(name, 'ok', job.clock.now())
    due, entered, release = Event(), Event(), Event()
    calls, catchups = [], []
    slot = at('20T11:15')

    def discover(*args, **kwargs):
        calls.append('discover')
        entered.set()
        assert release.wait(5)
        return {'state': 'ok', 'sources': {name: {'status': 'ok'} for name in scheduled.seeds.SOURCES}}

    def launch(name, due_slot):
        assert (name, due_slot) == ('seed_discovery', slot)
        due.set()
        assert entered.wait(5)
        result = job.run_job(name, catchup=due_slot)
        catchups.append(result)
        return Wait(lambda: result[0])

    monkeypatch.setattr(scheduled.seeds, 'discover', discover)
    monkeypatch.setattr(job, 'start_payload', lambda name, slot, fds:
                        Wait(lambda: int(not job.payload(name, slot, fds))))
    with ThreadPoolExecutor(2) as workers:
        watchdog = workers.submit(job.watchdog, launch)
        assert due.wait(5)
        ordinary = workers.submit(job.run_job, 'seed_discovery')
        try:
            watchdog.result(timeout=5)
            assert catchups == [(scheduled.DEFERRED, 'already_running')]
        finally:
            release.set()
        assert ordinary.result(timeout=5) == (0, 'complete')
    assert calls == ['discover']
    assert scheduled.stamp(job.stamps / 'seed_discovery.attempt') == slot
    assert job.run_job('seed_discovery', catchup=slot) == (0, 'slot_complete')


def test_watchdog_latest_strict_grace_and_rollover(job, monkeypatch):
    ready(job, monkeypatch)
    calls = []

    def start(name, slot, _):
        calls.append((name, slot))
        if name == 'wide_collect':
            checkpoint(job, slot)
        return Wait(lambda: 0)

    monkeypatch.setattr(job, 'start_payload', start)
    for now in ('20T09:20', '20T09:25'):
        job.clock.start = at(now)
        assert not job.eligible(('seed_discovery', 'wide_collect'))
    job.clock.start = at('20T11:00')
    job.watchdog(fake_launch(job))
    assert dict(calls)['seed_discovery'] == dict(calls)['wide_collect'] == at('20T09:15')
    assert len(calls) == 4
    job.watchdog(fake_launch(job))
    assert len(calls) == 4
    assert scheduled.stamp(job.stamps / 'seed_discovery.ok') == at('20T11:00')
    assert JOBS['seed_discovery'].latest(at('21T00:00')) == at('20T23:15')
    assert JOBS['daily_collect'].latest(at('21T00:00')) == at('20T04:15')


def test_admission_orders_shared_jobs_during_watchdog_and_ordinary_race(job, monkeypatch):
    ready(job, monkeypatch)
    job.clock.start = at('20T04:50')
    shared, independent = [], set()
    entered, finish, independent_done = Event(), Event(), Event()

    def start(name, slot, _):
        for path in job.payload_paths(name):
            with pytest.raises(Busy), lock(path):
                pytest.fail('payload lock was available at dispatch')
        if name in scheduled.SHARED_JOBS:
            shared.append((name, slot))
        else:
            independent.add(name)
            if len(independent) == 2:
                independent_done.set()
        if name == 'wide_collect':
            checkpoint(job, slot)
            entered.set()
            return Wait(lambda: 0 if finish.wait(timeout=10) else 1)
        return Wait(lambda: 0)

    # At :50 the latest twin slot is still inside grace. Its ordinary run is independent.
    monkeypatch.setattr(job, 'start_payload', start)
    with lock(job.settings.data_dir / 'daemon.lock'):
        job.watchdog(fake_launch(job))
        assert not set(job.stamps.glob('*.attempt')).intersection(
            job.stamps / f'{name}.attempt' for name in scheduled.SHARED_JOBS)
        assert shared == []
        assert job.run_job('active') == (0, 'older_slot_eligible')
    with ThreadPoolExecutor(3) as workers:
        first = workers.submit(job.watchdog, fake_launch(job))
        try:
            assert entered.wait(10)
            second = workers.submit(job.watchdog, fake_launch(job))
            assert job.run_job('active') == (0, 'older_slot_eligible')
            assert job.run_job('twin') == (0, 'complete')
            assert independent_done.wait(10)
            second.result(timeout=10)
        finally:
            finish.set()
        first.result(timeout=10)
    assert shared == [('wide_collect', at('20T03:15')), ('daily_collect', at('20T04:15')),
                      ('active', at('20T04:35'))]


def test_ordinary_failure_records_attempt_and_frees_next_active(job, monkeypatch):
    calls = []

    def start(name, slot, _):
        calls.append((name, slot))
        return Wait(lambda: 0 if name == 'active' else 1)

    monkeypatch.setattr(job, 'start_payload', start)
    job.clock.start = at('20T06:18')
    daily_slot = at('20T04:15')
    assert job.run_job('daily_collect') == (1, 'payload_incomplete')
    assert scheduled.stamp(job.stamps / 'daily_collect.attempt') == daily_slot
    assert not (job.stamps / 'daily_collect.ok').exists()
    assert job.run_job('daily_collect', catchup=daily_slot) == (0, 'slot_complete')
    assert calls == [('daily_collect', daily_slot)]

    job.clock.start = at('20T06:35')
    assert job.run_job('active') == (0, 'complete')
    assert scheduled.stamp(job.stamps / 'active.attempt') == at('20T06:35')
    assert job.run_job('active') == (0, 'slot_complete')
    assert calls == [('daily_collect', daily_slot), ('active', at('20T06:35'))]

    job.clock.start = at('21T04:26')
    assert job.eligible(('daily_collect',)) == [(at('21T04:15'), 'daily_collect')]


def test_fresh_install_ignores_previous_day_daily_slot(job, started_payloads):
    job.clock.start = at('20T03:35')
    assert job.eligible(('daily_collect',)) == [(at('19T04:15'), 'daily_collect')]
    assert job.run_job('active') == (0, 'complete')
    assert started_payloads == [('active', at('20T03:35'))]
    assert scheduled.stamp(job.stamps / 'active.attempt') == at('20T03:35')


def test_readiness_and_locks_are_not_dispatch_history(job, monkeypatch, started_payloads):
    ready(job, monkeypatch)
    replace(job.checkpoint, json_bytes({'stale': True}))
    (job.stamps / 'admission-collector.lock').touch()
    job.clock.start = at('20T04:35')
    assert job.eligible(scheduled.SHARED_JOBS) == [
        (at('20T03:15'), 'wide_collect'), (at('20T04:15'), 'daily_collect')]
    assert job.run_job('active') == (0, 'complete')
    assert started_payloads == [('active', at('20T04:35'))]
    assert scheduled.stamp(job.stamps / 'active.attempt') == at('20T04:35')


@pytest.mark.parametrize('kind', ['ok', 'attempt'])
def test_missed_daily_defers_active_one_slot_then_active_runs(job, started_payloads, kind):
    job.write_stamp('active', kind, at('20T03:35'))
    job.clock.start = at('20T04:35')
    assert job.run_job('active') == (0, 'older_slot_eligible')
    assert started_payloads == []
    assert scheduled.stamp(job.stamps / 'active.attempt') == (
        at('20T03:35') if kind == 'attempt' else None)
    assert not (job.stamps / 'daily_collect.ok').exists()
    assert not (job.stamps / 'daily_collect.attempt').exists()

    job.clock.start = at('20T05:35')
    assert job.run_job('active') == (0, 'complete')
    assert started_payloads == [('active', at('20T05:35'))]
    assert scheduled.stamp(job.stamps / 'active.attempt') == at('20T05:35')


def test_late_ordinary_active_does_not_defer_to_itself(job, started_payloads):
    job.write_stamp('daily_collect', 'ok', at('20T04:15'))
    job.clock.start = at('20T04:50')
    assert job.eligible(('active',)) == [(at('20T04:35'), 'active')]
    assert job.run_job('active') == (0, 'complete')
    assert started_payloads == [('active', at('20T04:35'))]
    assert scheduled.stamp(job.stamps / 'active.attempt') == at('20T04:35')
    assert scheduled.stamp(job.stamps / 'active.ok') == at('20T04:50')


def test_active_scans_all_peers_for_an_in_window_slot(job, monkeypatch, started_payloads):
    ready(job, monkeypatch)
    job.write_stamp('active', 'ok', at('20T03:35'))
    job.clock.start = at('20T04:35')
    assert job.eligible(scheduled.SHARED_JOBS) == [
        (at('20T03:15'), 'wide_collect'), (at('20T04:15'), 'daily_collect')]
    assert job.run_job('active') == (0, 'older_slot_eligible')
    assert started_payloads == []
    assert not (job.stamps / 'active.attempt').exists()


def test_stale_peer_outside_window_does_not_block_active(job, monkeypatch, started_payloads):
    ready(job, monkeypatch)
    job.write_stamp('daily_collect', 'ok', at('20T04:15'))
    job.write_stamp('active', 'ok', at('20T03:35'))
    job.clock.start = at('20T04:35')
    assert job.eligible(scheduled.SHARED_JOBS) == [(at('20T03:15'), 'wide_collect')]
    assert job.run_job('active') == (0, 'complete')
    assert started_payloads == [('active', at('20T04:35'))]


def test_unready_wide_slot_inside_window_does_not_block_active(job, started_payloads):
    job.write_stamp('daily_collect', 'ok', at('20T04:15'))
    job.clock.start = at('20T05:35')
    assert JOBS['wide_collect'].latest(kst_now(job.clock)) == at('20T05:15')
    assert job.eligible(scheduled.SHARED_JOBS) == []
    assert job.run_job('active') == (0, 'complete')
    assert started_payloads == [('active', at('20T05:35'))]


def test_unreadable_independent_stamp_counts_as_dispatch_history(job, started_payloads):
    replace(job.stamps / 'seed_discovery.attempt', b'invalid\n')
    job.clock.start = at('20T04:35')
    assert job.run_job('active') == (0, 'older_slot_eligible')
    assert started_payloads == []
    assert not (job.stamps / 'active.attempt').exists()


def test_failed_ordinary_records_attempt_and_killed_catchup_spends_slot(job, monkeypatch):
    job.clock.start = at('20T11:01')
    slot = JOBS['twin'].latest(kst_now(job.clock))
    monkeypatch.setattr(job, 'start_payload', lambda *_: Wait(lambda: 1))
    assert job.run_job('twin') == (1, 'payload_incomplete')
    assert scheduled.stamp(job.stamps / 'twin.attempt') == slot
    assert not (job.stamps / 'twin.ok').exists()
    assert job.run_job('twin', catchup=slot) == (0, 'slot_complete')

    job.clock.start = at('20T12:01')
    next_slot = at('20T11:50')

    def kill(_name, _slot, fds):
        return subprocess.Popen([sys.executable, '-c', 'import os,signal; os.kill(os.getpid(), signal.SIGKILL)'],
                                pass_fds=tuple(fds))

    monkeypatch.setattr(job, 'start_payload', kill)
    assert job.run_job('twin', catchup=next_slot)[0] == 128 + signal.SIGKILL
    assert scheduled.stamp(job.stamps / 'twin.attempt') == next_slot
    assert not (job.stamps / 'twin.ok').exists()
    assert job.run_job('twin', catchup=next_slot) == (0, 'slot_complete')


@pytest.mark.parametrize('status, success', [('partial:license_response', False), ('skipped:invalid_key', False),
                                            ('skipped:no_key', True), ('tavily_transport', False)])
def test_seed_payload_requires_complete_adapters(job, monkeypatch, status, success):
    connect(job.metrics.active_path).close()
    monkeypatch.setattr(scheduled.seeds, 'discover', lambda *_, **kw: {
        'state': 'ok', 'sources': {name: {'status': status} for name in scheduled.seeds.SOURCES}})
    paths = (job.stamps / 'seed_discovery.lock', *job.payload_paths('seed_discovery'))
    with ExitStack() as stack:
        fds = [stack.enter_context(lock(path)) for path in paths]
        assert job.payload('seed_discovery', at('20T09:15'), fds) is success


@pytest.mark.parametrize('name', ['active', 'seed_discovery', 'daily_collect', 'wide_collect'])
def test_scheduled_payload_does_not_bootstrap_authorization(job, name):
    paths = (job.stamps / f'{name}.lock', *job.payload_paths(name))
    with ExitStack() as stack:
        fds = [stack.enter_context(lock(path)) for path in paths]
        job.payload(name, at('20T09:15'), fds)
    assert not job.metrics.active_path.exists()


def test_wrappers_manual_seed_and_twin_timeout(job, monkeypatch):
    bin_dir = job.root / 'bin'
    bin_dir.mkdir()
    trace = job.root / 'manual'
    fake = bin_dir / 'python3'
    fake.write_text(f'#!{sys.executable}\n'
                    'import os, sys\nfrom pathlib import Path\n'
                    "if sys.argv[2:5] == ['hanset_active', 'seeds', 'discover']:\n"
                    "    with Path(os.environ['TRACE']).open('a') as file:\n"
                    "        file.write(' '.join(sys.argv[5:]) + '\\n')\n"
                    'else:\n'
                    '    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n')
    fake.chmod(0o755)
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'],
               HANSET_DATA_DIR=str(job.root), HANSET_ACTIVE_DIR=str(job.settings.active_dir),
               HANSET_IG_DIR=str(job.settings.data_dir), HANSET_TWIN_DIR=str(job.settings.twin_dir), TRACE=str(trace))
    seed, twin = [REPO_ROOT / 'scripts' / f'run_{name}_cron.sh' for name in ('seed_discovery', 'twin')]
    for present in (False, True):
        if present:
            job.write_stamp('seed_discovery', 'ok', at('20T11:00'))
            job.write_stamp('seed_discovery', 'attempt', at('20T09:15'))
        before = {path.name: path.read_bytes() for path in job.stamps.glob('seed_discovery.*')}
        for args in (['--source', 'licenses'], ['--max-calls', '0']):
            assert subprocess.run([seed, *args], env=env, timeout=15).returncode == 0
        assert {path.name: path.read_bytes() for path in job.stamps.glob('seed_discovery.*')} == before
    assert trace.read_text().splitlines() == ['--source licenses', '--max-calls 0'] * 2
    timeout = bin_dir / 'timeout'
    timeout.write_text('#!/bin/sh\nexit 124\n')
    timeout.chmod(0o755)
    assert subprocess.run([twin], env=env, timeout=15).returncode == 124
    assert not (job.stamps / 'twin.ok').exists()


def test_transfer_complete_only_after_stream_finishes(job, monkeypatch):
    class Interrupted:
        status_code = 200
        headers = {'Content-Type': 'text/html'}

        def iter_content(self, _):
            yield b'incomplete'
            raise requests.exceptions.ChunkedEncodingError()

        def close(self):
            pass

    def get(session, url, **kwargs):
        assert session.headers['User-Agent'] == USER_AGENT
        return Interrupted() if url == A else response(body='Complete transfer ' * 10)

    monkeypatch.setattr(requests.Session, 'get', get)
    with closing(connect(job.metrics.active_path)) as db:
        fetcher = Fetcher(Settings(data_dir=job.settings.active_dir, testing=True), db, job.clock,
                          public_scope=True, policy=COLLECTION)
        try:
            assert fetcher.fetch(A).outcome == 'error'
            assert fetcher.fetch(B).ok
        finally:
            fetcher.close()
        assert [tuple(row) for row in db.execute(
            'SELECT status,transfer_complete FROM request_log ORDER BY request_id')] == [(200, 0), (200, 1)]


def test_web_reads_live_targets_and_drains_due_retries(job, monkeypatch, capsys):
    enable_qwen(monkeypatch)
    ready(job, monkeypatch)
    calls = []
    real_web = scheduled.run_web
    monkeypatch.setattr(scheduled, 'run_web', lambda *args, **kwargs: real_web(
        *args, **kwargs, qwen_session=WebModel(lambda items, _: _hours_answer(items))))

    def get(session, url, **options):
        calls.append(url)
        if url == C:
            return response(403)
        if url == B and job.clock.now() < at('20T05:15'):
            raise requests.Timeout()
        return response(body=_page())

    monkeypatch.setattr(requests.Session, 'get', get)
    path = job.settings.data_dir / 'web_targets.json'
    path.parent.mkdir()
    job.clock.start = at('20T04:15')
    _targets(path, A, name='Seed Spa A', venue_id='sample', venue_key='sample', official_urls=[A, B, C])
    replace(path.parent / 'promotion_policy.json', json_bytes({'schema_version': 1, 'approvals': [approval(A)]}))
    connect(job.metrics.active_path).close()
    job.write_stamp('wide_collect', 'ok', at('20T03:20'))
    job.write_stamp('active', 'ok', at('20T03:36'))
    monkeypatch.setattr(job, 'start_payload', lambda name, slot, fds:
                        Wait(lambda: int(not job.payload(name, slot, fds))))
    assert job.run_job('daily_collect') == (1, 'payload_incomplete')
    result = json.loads(capsys.readouterr().out)
    assert result['state'] == 'partial' and result['reason'] == 'transport_retry_pending'
    assert result['promotion'] == {'reason': 'ok', 'changed_candidates': 1}
    assert calls == [A, B, C]
    with closing(claim_connect(path.parent)) as db:
        assert db.execute('SELECT qualification_basis FROM fact_candidates').fetchone()[0] == 'official'
        assert db.execute('SELECT count(*) FROM fact_candidate_claims').fetchone()[0] == 1
    with closing(connect(job.metrics.active_path)) as active:
        guard = scheduled.RunLock(active, job.settings.active_dir, 'test', clock=job.clock)
        with guard:
            job.clock.start = at('20T05:15')
            _targets(path, A, name='Seed Spa A', venue_id='sample', venue_key='sample', official_urls=[A, B, C, D])
            assert job.collect('wide_collect', at('20T05:15'), guard)
            assert calls == [A, B, C, B, D]
    with closing(claim_connect(path.parent)) as db:
        quote, locator = db.execute('SELECT quote,locator_json FROM claims WHERE source_url=?', (A,)).fetchone()
        assert quote == '' and json.loads(locator)['grounding']['version'] == 1
def export_fixture(settings, ids):
    root = settings.data_dir / 'official-web-changes'
    root.mkdir(parents=True, exist_ok=True)
    artifacts = {
        ARTIFACTS[0]: canonical_bytes({'proposals': []}),
        ARTIFACTS[1]: canonical_bytes({'candidates': [{'latest_change_ids': ids}]}),
        ARTIFACTS[2]: b'',
    }
    manifest = {**COMMON, 'generator_version': DETECTOR_VERSION, 'event_watermark': len(ids),
                'event_digest': sha(b'evidence'), 'artifacts': [
                    {'name': name, 'sha256': sha(data), 'bytes': len(data)} for name, data in artifacts.items()]}
    for name, content in artifacts.items():
        replace(root / name, content)
    value = canonical_bytes(manifest)
    replace(root / 'manifest.json', value)
    return {'reason': 'ok', 'event_watermark': len(ids), 'manifest_sha256': sha(value)}


class FakeOperations:
    """Exercise the proposed consumer contract with a fixture ledger keyed by change ID."""
    def __init__(self, outbox, current, path):
        self.outbox, self.current, self.path = outbox, current, path

    def consume(self, request):
        assert self.outbox.verify_generation(request['generation_digest']) == request['change_ids']
        self.outbox.validate_request(request)
        ledger = json.loads(self.path.read_text()) if self.path.exists() else {}
        release = []
        for change_id in request['change_ids']:
            facts = self.current[change_id]
            if (facts['official'] and facts['checked'] and facts['source'].startswith('https://')
                    and facts['mapped'] and not facts['hold'] and change_id not in ledger):
                release.append(change_id)
                ledger[change_id] = 'in_flight'
        replace(self.path, json_bytes(ledger))
        return release


def test_publication_readiness_checkpoint_and_overlapping_receipts(job, monkeypatch):
    ids = sorted(sha(value.encode()) for value in ('A', 'B', 'C', 'D', 'E'))
    a, b, c, d, e = ids
    assert job.eligible(('wide_collect',)) == []
    assert job.run_job('wide_collect', catchup=at('20T09:15'))[0] == 0
    assert not (job.stamps / 'wide_collect.attempt').exists()
    ready(job, monkeypatch)
    slot = at('20T09:15')
    export = export_fixture(job.settings, [a, b])
    request = job.outbox.prepare(job.settings.data_dir, export, slot)
    assert job.outbox.prepare(job.settings.data_dir, export, slot) == request
    checkpoint(job, slot, request)
    job.write_stamp('wide_collect', 'attempt', slot)
    expose = job.outbox.expose

    def interrupt(row):
        expose(row)
        raise OSError('simulated crash after exposure')

    monkeypatch.setattr(job.outbox, 'expose', interrupt)
    with pytest.raises(OSError):
        job.finish_wide()
    assert not (job.stamps / 'wide_collect.ok').exists()
    assert job.checkpoint.exists()
    monkeypatch.setattr(job.outbox, 'expose', expose)
    job.clock.start = at('20T11:16')
    assert job.finish_wide()
    assert scheduled.stamp(job.stamps / 'wide_collect.ok') == at('20T11:00')
    assert not job.accounted_for('wide_collect', at('20T11:15'))

    current = {cid: {'official': True, 'source': A, 'checked': '2026-11-20',
                     'mapped': True, 'hold': False} for cid in ids}
    ops = FakeOperations(job.outbox, current, job.root / 'ops' / 'ledger.json')
    assert ops.consume(request) == [a, b]
    assert ops.consume(request) == []
    current[d]['official'] = False
    current[e]['hold'] = True
    next_slot = at('20T11:15')
    second = job.outbox.prepare(job.settings.data_dir, export_fixture(job.settings, [b, c, d, e]), next_slot)
    assert FakeOperations(job.outbox, current, ops.path).consume(second) == [c]
    artifact = job.outbox.generations / second['generation_digest'] / ARTIFACTS[0]
    artifact.write_bytes(b'invalid')
    with pytest.raises(ValueError):
        job.outbox.expose(second)


def test_wide_stamps_and_metrics_close_with_absent_or_pending_receipts(job, monkeypatch):
    ready(job, monkeypatch)
    job.clock.start = at('20T23:27')
    # Isolate the wide slot from the older shared slots.
    for name in ('daily_collect', 'active', 'seed_discovery', 'twin'):
        job.write_stamp(name, 'ok', at('20T23:27'))
    slot = at('20T23:15')
    request = job.outbox.prepare(job.settings.data_dir, export_fixture(job.settings, [sha(b'A')]), slot)

    def start(name, started, fds):
        assert name == 'wide_collect'
        checkpoint(job, started, request)
        return Wait(lambda: 0)

    monkeypatch.setattr(job, 'start_payload', start)
    assert job.run_job('wide_collect', catchup=slot) == (0, 'complete')
    assert job.run_job('wide_collect', catchup=slot) == (0, 'slot_complete')
    pending = job.outbox.receipts / '2026-11-20' / request['request_id'] / 'pending.json'
    replace(pending, json_bytes({'schema_version': 1, 'request_id': request['request_id'],
                                 'generation_digest': request['generation_digest'], 'status': 'pending'}))
    job.clock.start = at('20T23:30')
    job.metrics.run()
    assert json.loads(job.metrics.output.read_text())['deploys'] == 0


def test_metrics_cutoff_rollover_and_partial_append(job, monkeypatch):
    job.clock.start = at('20T23:29')
    path = job.settings.data_dir / 'web_targets.json'
    path.parent.mkdir()
    _targets(path, A, name='Seed Spa A', venue_id='sample', venue_key='sample')
    policy = job.settings.data_dir / 'promotion_policy.json'
    replace(policy, json_bytes({'schema_version': 1, 'approvals': [approval(A)]}))
    request = job.outbox.prepare(job.settings.data_dir, export_fixture(job.settings, [sha(b'A')]), at('20T23:15'))
    job.outbox.expose(request)

    def receipt(name, status, completed, recorded, attempt):
        out = job.outbox.receipts / '2026-11-20' / request['request_id'] / f'{name}.json'
        replace(out, json_bytes({'schema_version': 1, 'request_id': request['request_id'],
                                 'generation_digest': request['generation_digest'], 'status': status,
                                 'completed_at': at(completed).isoformat(), 'recorded_at': at(recorded).isoformat(),
                                 'deployment_attempt_id': attempt}))

    receipt('one', 'deployed', '20T22:30', '20T22:35', 'deployment-a')
    receipt('replay', 'deployed', '20T22:30', '20T22:45', 'deployment-a')
    receipt('failed', 'failed', '20T23:17', '20T23:20', 'deployment-b')
    receipt('late', 'deployed', '20T23:17', '20T23:30', 'deployment-c')
    with closing(connect(job.metrics.active_path)) as writer, closing(claim_connect(path.parent)) as claims:
        with writer:
            writer.execute("INSERT INTO run_checkpoints(checkpoint_id,cursor) VALUES ('seed_freeze','current')")
            for freeze in ('current', 'prior'):
                writer.execute('INSERT INTO seed_domains(seed_domain_id,seed_id,domain) VALUES (?,?,?)',
                               (freeze, freeze, 'sample.test'))
                writer.execute('INSERT INTO seed_venues(seed_venue_id,seed_id) VALUES (?,?)', (freeze, freeze))
            for cid, first in (('old', at('19T12:00')), ('new', at('20T09:00'))):
                writer.execute("""INSERT INTO seed_candidates
                    (id,name,category,address,sido,sigungu,source_kind,source_url,first_seen,last_seen,status,
                     normalized_name,normalized_address) VALUES (?, 'Seed Spa A','sauna','','','','web',?,?,?,'new','','')""",
                    (cid, A, first.isoformat(), at('20T22:00').isoformat()))
            writer.executemany("""INSERT INTO request_log(url,domain,ts,status,kind,bytes,transfer_complete)
                                  VALUES (?, 'example.test',?,200,'body',5,?)""",
                               [(A, at('20T20:00').isoformat(), 1), (B, at('20T20:00').isoformat(), 0),
                                (C, at('20T23:30').isoformat(), 1)])
            writer.executemany('INSERT INTO request_log(ts,kind,adapter) VALUES (?,?,?)', [
                (at('20T22:00').isoformat(), 'api', 'seeds:tavily'),
                (at('20T22:00').isoformat(), 'api', None),
                (at('20T22:00').isoformat(), 'body', 'seeds:tavily'),
                (at('20T23:30').isoformat(), 'api', 'seeds:tavily'),
            ])
        from hanset_active.network_store import open_audit
        with closing(open_audit(job.settings.active_dir)) as fallback, fallback:
            fallback.executemany('''INSERT INTO request_log(url,domain,ts,status,kind,bytes,transfer_complete)
                                    VALUES (?,'example.test',?,?,'body',5,1)''',
                                 [(B, at('20T20:00').isoformat(), 200), (C, at('20T20:00').isoformat(), 503)])
        target, = assemble_targets(job.settings, now=job.clock.now(), testing=True)['authorization']
        for url in (A, B, C):
            record(claims, kind='web', url=url, text_hash=digest(url), disposition='applied',
                   claims=[{'field': 'hours', 'value': {'hours': '09:00-17:00', 'scope': 'weekday'},
                            'quote': 'Sample confirmed opening hours', 'rule_id': 'sample.v1'}],
                   venue_id='sample', venue_key='sample', seen_at=job.clock.now().isoformat(), clock=job.clock,
                   **({'source_type': 'own_site', 'requested_url': A, 'final_url': A,
                       'authorization': source_authorization(target, A, A, testing=True),
                       'domains': target['domains'], 'testing': True} if url == A else {}))
        from hanset_active.collect.service import run_promote
        assert run_promote(job.settings, policy_path=policy, clock=job.clock)['changed_candidates'] == 1
        empty = path.parent / 'empty-policy.json'
        replace(empty, json_bytes({'schema_version': 1, 'approvals': []}))
        assert run_promote(job.settings, policy_path=empty, clock=job.clock)['changed_candidates'] == 1
        assert claims.execute('SELECT review_state FROM fact_candidates').fetchone()[0] == 'evidence_hold'
        with closing(runtime.connect(job.settings.active_dir)) as models:
            attempt = runtime.dispatched(models, 'deepseek', 'sample', 'probe', job.clock, 1, 1)
            runtime.finished(models, attempt, 'model_timeout')
            runtime.dispatched(models, 'qwen', 'sample', 'completion', job.clock, 1, 1)
        tavily = job.settings.data_dir / 'tavily_usage.json'

        def usage(count):
            replace(tavily, json_bytes({'schema_version': 2, 'budgets': {'tavily': {
                'days': {'2026-11-20': {'used': count}}, 'last_reason': 'ok'}}}))

        usage(3)
        job.metrics.run()
        assert not job.metrics.output.exists()
        assert set(job.metrics._report(at('20T23:30').date(), at('20T23:30').date())) == KEYS
        job.clock.start = at('21T00:05')
        usage(4)
        replace(policy, json_bytes({'schema_version': 1, 'approvals': []}))
        replace(path, json_bytes({'schema_version': 1, 'targets': []}))
        day = at('20T23:30').date()
        report = job.metrics._report(day, day)
        assert report == {'date': '2026-11-20', 'seeds_total': 2, 'seeds_new': 1, 'targets': 1,
                          'pages_fetched': 2, 'claims_new': 3, 'facts_total': 1, 'unverified_total': 2,
                          'deploys': 1, 'deploy_failures': 1, 'provider_calls': {'deepseek': 1, 'qwen': 1},
                          'tavily_used': 3, 'seed_domains_total': 1, 'seed_venues_total': 1,
                          'fact_candidates_total': 1, 'seed_tavily_calls': 1}
        line = json_bytes(report)
        replace(job.metrics.journal, json_bytes({'offset': 0, 'line': line.decode(),
                                                 'prefix_sha256': hashlib.sha256(b'').hexdigest()}))
        replace(job.metrics.output, line[:37])
        with ThreadPoolExecutor(3) as workers:
            list(workers.map(lambda _: job.metrics.run(), range(3)))
        assert job.metrics.output.read_bytes() == line
        assert json.loads(job.metrics._path(job.clock.now().date()).read_text())['targets'] == 0
        job.metrics.run()
        assert job.metrics.output.read_bytes() == line
        replace(job.metrics._path(job.clock.now().date()), b'broken')
        job.clock.start = at('21T23:30')
        job.metrics.run()
        with writer:
            writer.execute("UPDATE seed_candidates SET status='resolved' WHERE id='new'")
        assert writer.execute("SELECT status FROM seed_candidates WHERE id='new'").fetchone()[0] == 'resolved'


def test_metrics_carries_sleep_days_and_a_nonzero_unobserved_bucket_can_retry(job, capsys):
    job.clock.start = at('20T23:29')
    job.metrics.run()
    tavily = job.settings.data_dir / 'tavily_usage.json'
    replace(tavily, json_bytes({'schema_version': 2, 'budgets': {'tavily': {
        'days': {'2026-11-21': {'used': 1}}, 'last_reason': 'ok'}}}))
    job.clock.start = at('23T00:10')
    job.metrics.run()
    lines = [json.loads(line) for line in job.metrics.output.read_text().splitlines()]
    assert [line['date'] for line in lines] == ['2026-11-20', '2026-11-22']
    assert lines[1]['tavily_used'] == lines[1]['seeds_total'] == 0
    assert 'metrics_tavily_cutoff_unknown 2026-11-21' in capsys.readouterr().err


def test_metrics_recovers_legacy_observations_and_pending_lines(job):
    day = at('20T23:29').date()
    replace(job.metrics.initialized, json_bytes({'date': day.isoformat()}))
    observed = {**dict.fromkeys(LEGACY_INVENTORY, 0), 'observed_at': at('20T23:29').isoformat()}
    replace(job.metrics._path(day), json_bytes(observed))
    legacy = job.metrics._report(day, day)
    assert legacy.keys() == LEGACY_KEYS
    assert not job.metrics._valid_line({**legacy, 'seed_tavily_calls': 1})
    line = json_bytes(legacy)
    replace(job.metrics.journal, json_bytes({'offset': 0, 'line': line.decode(),
                                             'prefix_sha256': hashlib.sha256(b'').hexdigest()}))
    replace(job.metrics.output, line[:37])
    job.clock.start = at('21T00:05')
    job.metrics.run()
    assert not job.metrics.journal.exists() and job.metrics.output.read_bytes() == line
    job.clock.start = at('21T23:30')
    job.metrics.run()
    old, new = job.metrics.output.read_bytes().splitlines(keepends=True)
    assert old == line and json.loads(new).keys() == KEYS


def test_guarded_active_persists_only_validated_evidence_and_twin_holds_missing(job, monkeypatch):
    settings, clock = job.settings, job.clock
    settings.active_dir.mkdir()
    calls = []

    def get(session, url, **kwargs):
        calls.append(url)
        assert session.headers['User-Agent'] == USER_AGENT
        return response(body='<title>Seed Spa A</title><main>Opening hours: 09:00-17:00. '
                             'Sensitive-fixture-body. ' + 'Guest facilities and sauna. ' * 10 + '</main>')

    monkeypatch.setattr(requests.Session, 'get', get)
    with closing(connect(job.metrics.active_path)) as db:
        vid = upsert_venue(db, 'Seed Spa A', website=A, clock=clock)
        tid = enqueue(db, kind='fetch', url=A, venue_id=vid, clock=clock)
        fetcher = Fetcher(Settings(data_dir=settings.active_dir, testing=True), db, clock,
                          public_scope=True, policy=COLLECTION)
        fetcher.facts_only = True
        try:
            executor = Executor(fetcher)
            executor.execute(dict(db.execute('SELECT * FROM tasks WHERE task_id=?', (tid,)).fetchone()))
            assert calls == [A]
            assert not (settings.active_dir / 'raw').exists()
            assert db.execute('SELECT blob_path FROM raw_documents').fetchone()[0] is None
            rows = db.execute('SELECT source_url,quote,locator FROM observations').fetchall()
            assert rows and all(row['source_url'] == A and not row['quote'] for row in rows)
            assert all(json.loads(row['locator'])['validation']['version'] == 1 for row in rows)
            assert 'Sensitive-fixture-body' not in '\n'.join(db.iterdump())
            with closing(twin_connect(settings.twin_dir / 'twin.sqlite3')) as twin:
                stages = [('fetch', incremental_fetch), ('changes', change_detect), ('extract', extract)]
                with db:
                    db.execute("UPDATE observations SET locator='{}' WHERE obs_id=(SELECT min(obs_id) FROM observations)")
                _, status, _ = run_once(twin, db, csv_path=settings.data_dir / 'absent.csv',
                                        lock_path=settings.twin_dir / 'daemon.lock', clock=clock,
                                        stages=stages, structured_only=True)
                assert status == 'ok'
                assert twin.execute("SELECT 1 FROM review_queue WHERE kind='active_validation'").fetchone()
        finally:
            fetcher.close()


@pytest.mark.parametrize('enabled', [False, True])
def test_daily_checkpoint_and_signal_only_on_opt_in(job, monkeypatch, enabled):
    monkeypatch.setenv('HANSET_AUTO_PUBLISH', '1' if enabled else '0')
    monkeypatch.setenv('HANSET_RUN_TOKEN', 'fixture-token')
    job.clock.start = at('20T04:16')
    job.write_stamp('active', 'ok', at('20T03:36'))
    slot = at('20T04:15')
    export = export_fixture(job.settings, [sha(b'fixture-change')])
    result = {'state': 'ok', 'success': True, 'changes': {'reason': 'ok', 'export': export}}
    monkeypatch.setattr(scheduled, 'run_web', lambda *args, **kwargs: result)
    monkeypatch.setattr(scheduled, 'public_result', lambda result: result)

    def start(name, started, _fds):
        return Wait(lambda: 0 if job.collect(name, started, None) else 1)

    monkeypatch.setattr(job, 'start_payload', start)
    assert not job.outbox.ready()
    assert job.run_job('daily_collect') == (0, 'complete')
    signal = job.stamps / 'daily_collect.publication.json'
    assert signal.exists() == enabled
    assert not job.daily_checkpoint.exists()
    if enabled:
        row = json.loads(signal.read_text())
        assert row['run_token'] == 'fixture-token' and row['slot'] == slot.isoformat()
        request = json.loads((job.outbox.requests / (row['request_id'] + '.json')).read_text())
        assert request['job'] == 'daily_collect'
        assert job.run_job('daily_collect') == (0, 'slot_complete')
        assert json.loads(signal.read_text()) == row
    else:
        assert not job.outbox.requests.exists()


def test_failed_promotion_rolls_back_and_preserves_scheduled_handoff(job, monkeypatch, capsys):
    from hanset_active.collect import service, store

    ready(job, monkeypatch)
    enable_qwen(monkeypatch)
    job.clock.start = at('20T05:16')
    job.write_stamp('daily_collect', 'ok', at('20T04:16'))
    job.write_stamp('active', 'ok', at('20T04:36'))
    connect(job.metrics.active_path).close()
    path = job.settings.data_dir / 'web_targets.json'
    path.parent.mkdir()
    _targets(path, A, venue_id='sample', venue_key='sample')
    replace(path.parent / 'promotion_policy.json', json_bytes({'schema_version': 1, 'approvals': [approval(A)]}))
    monkeypatch.setattr(requests.Session, 'get', lambda *_args, **_kwargs: response(body=_page()))
    real_web = scheduled.run_web
    monkeypatch.setattr(scheduled, 'run_web', lambda *args, **kwargs: real_web(
        *args, **kwargs, qwen_session=WebModel(lambda items, _: _hours_answer(items))))
    export = export_fixture(job.settings, [sha(b'fixture-change')])
    monkeypatch.setattr(service, '_change_cycle', lambda *args, **kwargs: {'reason': 'ok', 'export': export})
    persist = store.persist_candidates

    def fail(db, candidates, *, at):
        with pytest.raises(Busy), lock(job.settings.data_dir / 'daemon.lock'):
            pytest.fail('scheduled promotion lost its inherited lock')
        assert persist(db, candidates, at=at) == 1
        raise RuntimeError('fixture private failure')

    monkeypatch.setattr(store, 'persist_candidates', fail)
    monkeypatch.setattr(job, 'start_payload', lambda name, slot, fds:
                        Wait(lambda: int(not job.payload(name, slot, fds))))
    assert job.run_job('wide_collect') == (0, 'complete')
    output = capsys.readouterr().out
    assert 'fixture private failure' not in output
    result = json.loads(output)
    assert result['success'] and result['collected']
    assert result['promotion'] == {'reason': 'promotion_write_failed'}
    signal = json.loads((job.stamps / 'wide_collect.publication.json').read_text())
    assert (job.outbox.requests / (signal['request_id'] + '.json')).is_file()
    with closing(claim_connect(job.settings.data_dir)) as db:
        assert db.execute('SELECT count(*) FROM claims').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM fact_candidates').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM fact_candidate_claims').fetchone()[0] == 0
