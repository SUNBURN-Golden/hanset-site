"""Crash, restart, process contention and KST boundary tests with no network."""
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from hanset_active.config import REPO_ROOT
from hanset_active.search import budget, tavily
from hanset_active.util.timeutil import FakeClock


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch, isolate_tavily_credentials):
    monkeypatch.setenv('HANSET_IG_DIR', str(tmp_path / 'ig'))
    monkeypatch.setenv('TAVILY_API_KEY', 'sentinel-budget-private-value')
    monkeypatch.setattr(tavily, '_KEY_PATH', tmp_path / 'absent-key')


def state_path():
    return Path(os.environ['HANSET_IG_DIR']) / 'tavily_usage.json'


def reserve(clock=None):
    with budget.locked_budget(clock=clock or FakeClock()) as reservation:
        allowed = reservation.reserve()
        return allowed, reservation.reason, reservation.snapshot


def test_first_thirty_then_exhaustion_and_restart():
    for used in range(1, 31):
        allowed, reason, snapshot = reserve()
        assert allowed and reason == 'ok' and snapshot.used == used
    assert reserve()[1] == 'tavily_budget_exhausted'
    result = subprocess.run([sys.executable, '-c', '''
from hanset_active.search.budget import locked_budget
from hanset_active.util.timeutil import FakeClock
with locked_budget(clock=FakeClock()) as r:
    assert not r.reserve()
    print(r.reason)
'''], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'tavily_budget_exhausted'
    state = json.loads(state_path().read_text())['budgets']['tavily']
    assert state['days']['2026-01-01']['used'] == 30
    assert state['last_reason'] == 'tavily_budget_exhausted'
    assert state_path().stat().st_mode & 0o777 == 0o600


def test_thirty_dispatches_then_zero_even_after_fresh_call():
    response = Mock(status_code=200)
    response.iter_content.return_value = [b'{"results":[]}']
    post = Mock(return_value=response)
    for used in range(1, 31):
        result = tavily._search('Known Sauna', 'official_page', clock=FakeClock(), transport=post)
        assert result.status == 'ok' and result.budget.used == used
    for _ in range(2):
        assert tavily._search('Known Sauna', 'official_page', clock=FakeClock(), transport=post).reason == 'tavily_budget_exhausted'
    assert post.call_count == 30


def _contend(directory):
    os.environ['HANSET_IG_DIR'] = directory
    successes = 0
    for _ in range(12):
        with budget.locked_budget(clock=FakeClock()) as r:
            if r.reserve() and r.ready_to_dispatch():
                # Increment a dispatch marker under the same lock as the request.
                with open(Path(directory) / 'dispatches', 'a') as stream:
                    stream.write('request\n')
                successes += 1
    return successes


def test_processes_cannot_exceed_thirty():
    directory = os.environ['HANSET_IG_DIR']
    with ProcessPoolExecutor(4, mp_context=multiprocessing.get_context('spawn')) as pool:
        counts = list(pool.map(_contend, [directory] * 4))
    assert sum(counts) == 30
    assert len((Path(directory) / 'dispatches').read_text().splitlines()) == 30
    assert json.loads(state_path().read_text())['budgets']['tavily']['days']['2026-01-01']['used'] == 30


def test_kst_midnight_resets_but_utc_midnight_does_not_and_rollback_retains():
    clock = FakeClock(datetime(2026, 11, 5, 14, 59, 59, tzinfo=timezone.utc))
    for _ in range(30):
        assert reserve(clock)[0]
    assert reserve(clock)[1] == 'tavily_budget_exhausted'
    clock.sleep(1)
    assert reserve(clock)[2].day == '2026-11-06'
    assert reserve(clock)[2].used == 2
    clock.sleep(9 * 3600)
    assert reserve(clock)[2].used == 3  # UTC midnight is still the same KST day.
    rollback = FakeClock(datetime(2026, 11, 5, 14, tzinfo=timezone.utc))
    assert reserve(rollback)[1] == 'tavily_budget_exhausted'


def test_rollover_between_reservation_and_dispatch():
    clock = FakeClock(datetime(2026, 11, 5, 14, 59, 59, tzinfo=timezone.utc))
    with budget.locked_budget(clock=clock) as r:
        assert r.reserve()
        clock.sleep(1)
        assert r.ready_to_dispatch() and r.snapshot.day == '2026-11-06'
    days = json.loads(state_path().read_text())['budgets']['tavily']['days']
    assert days == {'2026-11-05': {'used': 1}, '2026-11-06': {'used': 1}}


def test_crash_after_reservation_remains_counted():
    result = subprocess.run([sys.executable, '-c', '''
import os
from hanset_active.search.budget import locked_budget
from hanset_active.util.timeutil import FakeClock
with locked_budget(clock=FakeClock()) as r:
    assert r.reserve()
    os._exit(9)
'''], capture_output=True)
    assert result.returncode == 9
    assert reserve()[2].used == 2


@pytest.mark.parametrize('mutation', ['json', 'used', 'limit', 'extra', 'duplicate', 'schema'])
def test_corrupt_state_prevents_dispatch(mutation):
    reserve()
    state = json.loads(state_path().read_text())
    tav = state['budgets']['tavily']
    if mutation == 'used':
        tav['days']['2026-01-01']['used'] = -1
    elif mutation == 'limit':
        tav['daily_limit'] = 999
    elif mutation == 'extra':
        tav['credential'] = 'sentinel-budget-private-value'
    elif mutation == 'schema':
        state['schema_version'] = True
    state_path().write_text('broken' if mutation == 'json' else '{"schema_version":1,"schema_version":1}' if mutation == 'duplicate' else json.dumps(state))
    post = Mock()
    result = tavily._search('Known Sauna', 'official_page', transport=post)
    assert result.reason == 'tavily_budget_unavailable'
    assert 'sentinel-budget-private-value' not in repr(result)
    post.assert_not_called()


@pytest.mark.parametrize('operation', ['fsync', 'replace', 'mkstemp', 'lock', 'directory_fsync'])
def test_persistence_or_lock_failure_prevents_dispatch(monkeypatch, operation):
    def fail(*args, **kwargs):
        raise OSError('sentinel-budget-private-value')
    if operation == 'lock':
        monkeypatch.setattr(budget.fcntl, 'flock', fail)
    elif operation == 'mkstemp':
        monkeypatch.setattr(budget.tempfile, operation, fail)
    elif operation == 'directory_fsync':
        original = os.fsync
        calls = []
        def second_fails(fd):
            calls.append(fd)
            if len(calls) == 2:
                fail()
            original(fd)
        monkeypatch.setattr(os, 'fsync', second_fails)
    else:
        monkeypatch.setattr(os, operation, fail)
    post = Mock()
    result = tavily._search('Known Sauna', 'official_page', transport=post)
    assert result.reason == 'tavily_budget_unavailable'
    assert 'sentinel-budget-private-value' not in repr(result)
    post.assert_not_called()
    assert not list(state_path().parent.glob('.tavily-*'))


@pytest.mark.parametrize('directory', ['missing', 'checkout', 'symlink'])
def test_invalid_state_directory_skips(monkeypatch, tmp_path, directory):
    if directory == 'missing':
        monkeypatch.delenv('HANSET_IG_DIR')
    else:
        configured = REPO_ROOT
        if directory == 'symlink':
            configured = tmp_path / 'repo-link'
            configured.symlink_to(REPO_ROOT, target_is_directory=True)
        monkeypatch.setenv('HANSET_IG_DIR', str(configured))
    post = Mock()
    assert tavily._search('Known Sauna', 'official_page', transport=post).reason == 'tavily_budget_unavailable'
    post.assert_not_called()


def test_interrupted_attempt_retains_reservation():
    post = Mock(side_effect=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        tavily._search('Known Sauna', 'official_page', clock=FakeClock(), transport=post)
    assert reserve()[2].used == 2
