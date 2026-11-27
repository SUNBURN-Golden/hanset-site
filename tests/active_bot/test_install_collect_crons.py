"""Exercise the installer against a byte-preserving fake crontab."""
import os
import subprocess
import sys

import pytest

from hanset_active.collect import cron_install
from hanset_active.collect.cron_command import cron_decode
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
        script = ('import os, sys; from hanset_active.collect import cron_install; '
                  'cron_install.scheduler_default = lambda: os.environ.get("HANSET_CRON_TIMEZONE"); '
                  'sys.exit(cron_install.main(sys.argv[2:], data_root=sys.argv[1]))')
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
              b'* * * * * # hanset-collx\n'
              b'@hourly /existing/task;# hanset-collx\r\n'
              b'@hourly /existing/task # hanset-collx %fixture-stdin\n')
    tail = b'@weekly /existing/unmanaged\n'
    seed = b'15 1 * * * /workspace/hanset-site/scripts/run_seed_discovery_cron.sh'
    stale_alarm = rb"@hourly case 'fixture\%' in *) /old/task fixture-private-value ;; esac # hanset-collx" + b'\n'
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
    state.write_bytes(expected + stale_alarm)
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


def test_seed_only_installs_and_reconciles_without_publication_readiness(crontab):
    root, state, writes, invoke = crontab
    entry = owned_entries()['seed_discovery']
    preview = invoke('--seed-only', '--dry-run')
    assert preview.returncode == 0 and preview.stderr == b''
    assert b'+' + entry in preview.stdout
    assert not state.exists() and not writes.exists()
    assert invoke('--seed-only').returncode == 0 and state.read_bytes() == entry

    early = b'CRON_TZ=UTC\r\n' + owned_entries()['watchdog'] + b'CRON_TZ="Asia/Seoul"\r\n'
    tail = owned_entries()['wide_collect'] + b'COMMENT=fixture-private-value\r\n@weekly /existing/job\n'
    stale = b'15 3 * * * /workspace/hanset-site/scripts/run_seed_discovery_cron.sh # hanset-collx\r\n'
    state.write_bytes(early + stale + tail + stale)
    preview = invoke('--seed-only', '--dry-run')
    assert preview.returncode == 0 and b'fixture-private-value' not in preview.stdout + preview.stderr
    assert state.read_bytes() == early + stale + tail + stale
    assert invoke('--seed-only').returncode == 0
    assert state.read_bytes() == early + entry + tail
    assert invoke('--seed-only').returncode == 0 and writes.read_bytes() == b'++'
    assert not (root / 'publication').exists()


def test_seed_only_checks_timezone_at_effective_entry(crontab):
    _, state, writes, invoke = crontab
    original = b'CRON_TZ=Asia/Seoul\r\n@weekly /existing/job\nCRON_TZ=UTC\n'
    state.write_bytes(original)
    result = invoke('--seed-only')
    assert result.returncode != 0 and result.stderr == b'scheduler_timezone_unverified_or_conflicting seed_discovery\n'
    assert state.read_bytes() == original and not writes.exists()


COLLECT = b'/workspace/hanset-site/scripts/run_collect_cron.sh'
WATCHDOG = b'/workspace/hanset-site/scripts/collect_watchdog.sh'
DAILY = b'15 04 * * *  '
OLD = b'fixture-old-setting'
NEW = b'fixture-rotated-setting'
FILE_BACKED = b'fixture-file-backed-setting'


def managed_with_proxy(job, prefix):
    script = WATCHDOG if job == 'watchdog' else COLLECT
    return owned_entries()[job].replace(script, prefix + b' ' + script)


@pytest.mark.parametrize('daily_publish', [False, True])
@pytest.mark.parametrize(('source_prefix', 'managed_prefix', 'expected'), [
    (b'''HANSET_WEB_PROXY='fixture-new' OTHER=stay HANSET_WEB_PROXY_EXIT_IP= '''
     rb'''HANSET_WEB_PROXY_HEAL_CMD='helper \% literal' /bin/sh''',
     rb'''HANSET_WEB_PROXY='fixture-new' HANSET_WEB_PROXY_EXIT_IP= HANSET_WEB_PROXY_HEAL_CMD='helper \% literal' ''',
     {'HANSET_WEB_PROXY': 'fixture-new', 'HANSET_WEB_PROXY_EXIT_IP': '',
      'HANSET_WEB_PROXY_HEAL_CMD': 'helper % literal'}),
    (rb'''HANSET_WEB_PROXY_EXIT_IP=fixture\ address OTHER=stay /usr/bin/env SKIP=stay '''
     rb''' 'HANSET_WEB_PROXY_HEAL_CMD=fixture helper \%' "HANSET_WEB_PROXY="''',
     rb'''HANSET_WEB_PROXY_EXIT_IP=fixture\ address /usr/bin/env 'HANSET_WEB_PROXY_HEAL_CMD=fixture helper \%' "HANSET_WEB_PROXY=" ''',
     {'HANSET_WEB_PROXY': '', 'HANSET_WEB_PROXY_EXIT_IP': 'fixture address',
      'HANSET_WEB_PROXY_HEAL_CMD': 'fixture helper %'}),
])
def test_proxy_install_reinstall_and_uninstall(crontab, daily_publish, source_prefix, managed_prefix, expected):
    root, state, writes, invoke = crontab
    ready(root)
    daily = DAILY + source_prefix + b' ' + COLLECT + b' web  # operator note\r\n'
    stale = managed_with_proxy('wide_collect', b'HANSET_WEB_PROXY=' + OLD)
    state.write_bytes(stale + daily + owned_entries()['wide_collect'])
    args = ('--daily-publish',) if daily_publish else ()
    preview = invoke(*args, '--dry-run')
    assert preview.returncode == 0 and preview.stderr == b''
    assert b'+[wide_collect]' in preview.stdout and b'+[watchdog]' in preview.stdout
    for value in (OLD, b'fixture-new', b'fixture helper', b'fixture\\ address', b'operator note'):
        assert value not in preview.stdout + preview.stderr
    assert not writes.exists()
    assert invoke(*args).returncode == 0
    installed = state.read_bytes()
    wide = managed_with_proxy('wide_collect', managed_prefix.rstrip())
    watchdog = managed_with_proxy('watchdog', managed_prefix.rstrip())
    decorated = DAILY + b'HANSET_AUTO_PUBLISH=1 ' + daily[len(DAILY):-2] + b' # hanset-collx-daily\r\n'
    assert installed == wide + (decorated if daily_publish else daily) + owned_entries()['seed_discovery'] + watchdog

    stub = state.parent / 'inspect-env'
    stub.write_text('#!/bin/sh\nexec /usr/bin/env\n')
    stub.chmod(0o755)
    for line in (daily, wide, watchdog):
        command, _, stdin = cron_decode(line.rstrip(b'\r\n').split(None, 5)[-1])
        assert not stdin
        command = command.replace(COLLECT, os.fsencode(stub)).replace(WATCHDOG, os.fsencode(stub))
        output = subprocess.check_output(['/bin/sh', '-c', command], env={'PATH': '/usr/bin:/bin'}, text=True)
        actual = dict(line.split('=', 1) for line in output.splitlines() if line.startswith('HANSET_WEB_PROXY'))
        assert actual == expected

    preview = invoke(*args, '--dry-run')
    assert (preview.returncode, preview.stdout, preview.stderr) == (0, b'', b'')
    assert invoke(*args).returncode == 0
    assert state.read_bytes() == installed and writes.read_bytes() == b'+'
    wrong_zone = b'CRON_TZ=UTC\n' + wide + b'CRON_TZ=Asia/Seoul\n' + installed[len(wide):]
    state.write_bytes(wrong_zone)
    result = invoke(*args)
    assert result.returncode == 1 and result.stderr == b'scheduler_timezone_unverified_or_conflicting wide_collect\n'
    assert state.read_bytes() == wrong_zone and writes.read_bytes() == b'+'

    state.write_bytes(installed)
    preview = invoke('--uninstall', '--dry-run')
    assert preview.returncode == 0 and preview.stdout.count(b'-[redacted] # hanset-collx') == 3
    assert b'helper' not in preview.stdout + preview.stderr
    assert invoke('--uninstall').returncode == 0
    assert state.read_bytes() == daily and writes.read_bytes() == b'++'


@pytest.mark.parametrize('keep_daily', [False, True])
def test_proxy_rotation_private_file_and_removal(crontab, keep_daily):
    root, state, _, invoke = crontab
    ready(root)
    daily = b'15 4 * * * ' + COLLECT + b'\n'
    prior = b'HANSET_WEB_PROXY=' + OLD
    state.write_bytes(daily.replace(COLLECT, prior + b' ' + COLLECT))
    assert invoke().returncode == 0
    updated = b'HANSET_WEB_PROXY_HEAL_CMD=' + NEW
    rotated = state.read_bytes().replace(prior, updated, 1)
    state.write_bytes(rotated)
    preview = invoke('--dry-run')
    assert preview.returncode == 0
    assert OLD not in preview.stdout + preview.stderr and NEW not in preview.stdout + preview.stderr
    assert invoke().returncode == 0
    assert state.read_bytes().count(updated) == 3 and prior not in state.read_bytes()

    private = state.parent / 'fixture private settings.json'
    replace(private, json_bytes({'HANSET_WEB_PROXY': FILE_BACKED.decode()}))
    selector = b"HANSET_COLLX_PROXY_ENV_FILE='" + os.fsencode(private) + b"'"
    state.write_bytes(state.read_bytes().replace(updated, selector, 1))
    preview = invoke('--dry-run')
    assert preview.returncode == 0
    for value in (OLD, NEW, FILE_BACKED, os.fsencode(private)):
        assert value not in preview.stdout + preview.stderr
    assert invoke().returncode == 0
    assert state.read_bytes().count(selector) == 3
    assert FILE_BACKED not in state.read_bytes()
    preview = invoke('--uninstall', '--dry-run')
    assert os.fsencode(private) not in preview.stdout + preview.stderr

    remaining = daily if keep_daily else b''
    state.write_bytes(remaining + b''.join(state.read_bytes().splitlines(keepends=True)[1:]))
    assert invoke().returncode == 0
    assert state.read_bytes() == remaining + b''.join(owned_entries().values())


def test_managed_daily_retains_its_assignments_until_disabled(crontab):
    root, state, writes, invoke = crontab
    ready(root)
    stock = owned_entries(daily_publish=True)
    proxy = rb"HANSET_WEB_PROXY_HEAL_CMD='managed \% fixture'"
    daily = stock['daily_collect'].replace(COLLECT, proxy + b' ' + COLLECT)
    state.write_bytes(daily)
    assert invoke('--daily-publish').returncode == 0
    installed = daily + stock['seed_discovery'] + managed_with_proxy('wide_collect', proxy) + managed_with_proxy('watchdog', proxy)
    assert state.read_bytes() == installed
    preview = invoke('--daily-publish', '--dry-run')
    assert (preview.returncode, preview.stdout, preview.stderr) == (0, b'', b'')
    assert invoke('--daily-publish').returncode == 0
    assert state.read_bytes() == installed and writes.read_bytes() == b'+'

    assert invoke().returncode == 0
    assert state.read_bytes() == b''.join(owned_entries().values())
    assert invoke('--daily-publish').returncode == 0
    assert state.read_bytes() == b''.join(owned_entries().values()) + stock['daily_collect']


def test_first_operator_daily_takes_precedence_over_managed_daily(crontab):
    root, state, _, invoke = crontab
    ready(root)
    managed = owned_entries(daily_publish=True)['daily_collect'].replace(COLLECT, b'HANSET_WEB_PROXY=managed ' + COLLECT)
    first = DAILY + b'HANSET_WEB_PROXY_CHECK_URL=first ' + COLLECT + b'\n'
    later = DAILY + b'HANSET_WEB_PROXY=later ' + COLLECT + b' web\n'
    unrelated = DAILY + rb"printf '\%s' HANSET_WEB_PROXY=fixture-unrelated " + COLLECT + b' web\r\n'
    state.write_bytes(managed + unrelated + first + later)
    assert invoke('--daily-publish').returncode == 0
    expected = (unrelated + DAILY + b'HANSET_AUTO_PUBLISH=1 ' + first[len(DAILY):-1] + b' # hanset-collx-daily\n'
                + DAILY + b'HANSET_AUTO_PUBLISH=1 ' + later[len(DAILY):-1] + b' # hanset-collx-daily\n'
                + owned_entries()['seed_discovery'] + managed_with_proxy('wide_collect', b'HANSET_WEB_PROXY_CHECK_URL=first')
                + managed_with_proxy('watchdog', b'HANSET_WEB_PROXY_CHECK_URL=first'))
    assert state.read_bytes() == expected


def test_publication_opt_in_and_reversal_cover_all_operator_dailies(crontab):
    root, state, writes, invoke = crontab
    ready(root)
    first = DAILY + COLLECT + b'\n'
    second = DAILY + b'/bin/bash ' + COLLECT + b' web\r\n'
    state.write_bytes(first + second)
    assert invoke('--daily-publish').returncode == 0
    stock = b''.join(owned_entries().values())
    decorated = (DAILY + b'HANSET_AUTO_PUBLISH=1 ' + first[len(DAILY):-1] + b' # hanset-collx-daily\n'
                 + DAILY + b'HANSET_AUTO_PUBLISH=1 ' + second[len(DAILY):-2] + b' # hanset-collx-daily\r\n')
    assert state.read_bytes() == decorated + stock
    assert invoke().returncode == 0
    assert state.read_bytes() == first + second + stock and writes.read_bytes() == b'++'


def test_managed_daily_restores_publication_assignment(crontab):
    root, state, writes, invoke = crontab
    ready(root)
    daily = owned_entries(daily_publish=True)['daily_collect']
    state.write_bytes(daily.replace(b'HANSET_AUTO_PUBLISH=1 ', b''))
    assert invoke('--daily-publish').returncode == 0
    assert state.read_bytes() == daily + b''.join(owned_entries().values())
    assert writes.read_bytes() == b'+'


@pytest.mark.parametrize('command', [
    b'HANSET_WEB_PROXY=$PROXY_URL ' + COLLECT,
    b'HANSET_WEB_PROXY=$(printf fixture-private-value) ' + COLLECT,
    b'env "HANSET_WEB_PROXY_HEAL_CMD=$(fixture-private-helper)" ' + COLLECT,
    b'env -i HANSET_WEB_PROXY=fixture-private-value ' + COLLECT,
    b'HANSET_WEB_PROXY=fixture-private-value ' + COLLECT + b' web%fixture-private-stdin',
    b'HANSET_COLLX_PROXY_ENV_FILE=fixture-relative-path ' + COLLECT,
])
def test_unsupported_proxy_source_leaves_crontab_intact(crontab, command):
    root, state, writes, invoke = crontab
    ready(root)
    original = owned_entries()['wide_collect'] + DAILY + command + b'\n'
    state.write_bytes(original)
    for args in (('--dry-run',), ('--daily-publish',)):
        result = invoke(*args)
        assert (result.returncode, result.stdout, result.stderr) == (1, b'', b'daily_proxy_settings_unsupported\n')
        assert state.read_bytes() == original and not writes.exists()


@pytest.mark.parametrize('prefix', [b'HANSET_WEB_PROXY= ', b'env "HANSET_WEB_PROXY_HEAL_CMD=fixture-helper" '])
def test_mixed_proxy_sources_leave_crontab_intact(crontab, prefix):
    root, state, writes, invoke = crontab
    ready(root)
    selector = b"HANSET_COLLX_PROXY_ENV_FILE='" + os.fsencode(root / 'fixture private settings.json') + b"' "
    original = owned_entries()['wide_collect'] + DAILY + selector + prefix + COLLECT + b'\n'
    state.write_bytes(original)
    for args in (('--dry-run',), ()):
        result = invoke(*args)
        assert (result.returncode, result.stdout, result.stderr) == (1, b'', b'daily_proxy_settings_unsupported\n')
        assert state.read_bytes() == original and not writes.exists()


def test_cronie_percent_pairs_and_effective_ownership():
    assert cron_decode(rb"'a\\\%b' %stdin") == (rb"'a\\%b' ", [0, 1, 2, 3, 5, 6, 7, 8], True)
    assert cron_decode(rb"'a\\%b' # hanset-collx")[0] == rb"'a\\"
    assert cron_install.owned(rb"@hourly /job 'fixture\%' # hanset-collx")
    assert not cron_install.owned(rb"@hourly /job 'fixture\%' # hanset-collx %stdin")
    assert not cron_install.owned(rb"@hourly /job 'fixture\\%' # hanset-collx")
    assert not cron_install.owned(rb"@hourly /job 'fixture\%' %stdin # hanset-collx")
    assert not cron_install.owned(rb"@hourly /job # earlier comment # hanset-collx")
