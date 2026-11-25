"""Cron wrapper: lock, private env loading, and no credential echo."""
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / 'scripts' / 'run_collect_cron.sh'
SENTINEL = 'sentinel-not-a-real-key'


def _env(tmp_path, ig):
    env = os.environ.copy()
    for name in ('DEEPSEEK_API_KEY', 'TAVILY_API_KEY', 'HANSET_DEEPSEEK_MODEL', 'HANSET_IG_SESSION'):
        env.pop(name, None)
    env['PATH'] = str(tmp_path / 'bin') + os.pathsep + env['PATH']
    env['HANSET_DATA_DIR'] = str(tmp_path)
    env['HANSET_IG_DIR'] = str(ig)
    env['HANSET_ACTIVE_DIR'] = str(tmp_path / 'active')
    env['HANSET_TWIN_DIR'] = str(tmp_path / 'twin')
    env['EXPECT_KEY'] = SENTINEL
    env['SIDE_FILE'] = str(tmp_path / 'side')
    env['READY'] = str(tmp_path / 'ready')
    return env


def _fake_python(tmp_path):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    program = bindir / 'python3'
    program.write_text(
        '#!' + sys.executable + '\n'
        'import os, sys, time\n'
        "if sys.argv[2:3] == ['hanset_active.collect.scheduled'] and '--payload' not in sys.argv:\n"
        '    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n'
        'from pathlib import Path\n'
        "key = os.environ.get('DEEPSEEK_API_KEY', '')\n"
        "expect = os.environ.get('EXPECT_KEY', '')\n"
        "Path(os.environ['SIDE_FILE']).write_text('yes' if key and key == expect else 'no')\n"
        "if os.environ.get('SLEEP') == '1':\n"
        "    Path(os.environ['READY']).write_text('up')\n"
        '    time.sleep(2)\n'
        'print(\'{"state":"done","success":true,"collected":false,"mode":"web","reason":"ok"}\')\n'
    )
    program.chmod(0o755)


def _log(ig):
    logs = sorted((ig / 'logs').glob('collect-*.log'))
    assert logs
    return '\n'.join(path.read_text() for path in logs)


def test_wrapper_syntax_and_secret_loading(tmp_path):
    syntax = subprocess.run(['bash', '-n', str(SCRIPT)], check=False)
    assert syntax.returncode == 0
    unsupported = subprocess.run(['bash', str(SCRIPT), 'browse'], check=False)
    assert unsupported.returncode == 2
    ig = tmp_path / 'ig'
    _fake_python(tmp_path)
    env = _env(tmp_path, ig)
    secret = ig / 'deepseek.env'
    ig.mkdir()
    secret.write_text('export DEEPSEEK_API_KEY=' + SENTINEL + '\necho "$DEEPSEEK_API_KEY"\n')
    secret.chmod(0o644)
    loose = subprocess.run(['bash', str(SCRIPT)], env=env, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert loose.returncode == 1
    assert 'credential_file_permissions' in _log(ig)
    assert SENTINEL not in _log(ig) and SENTINEL not in loose.stdout and SENTINEL not in loose.stderr
    assert not (tmp_path / 'side').exists()
    secret.chmod(0o600)
    link = ig / 'linked.env'
    secret.rename(link)
    secret.symlink_to(link)
    linked = subprocess.run(['bash', str(SCRIPT)], env=env, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert linked.returncode == 1 and 'credential_file_symlink' in _log(ig)
    assert SENTINEL not in linked.stdout and SENTINEL not in linked.stderr
    secret.unlink()
    link.rename(secret)
    secret.chmod(0o600)
    loaded = subprocess.run(['bash', str(SCRIPT)], env=env, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert loaded.returncode == 0
    assert (tmp_path / 'side').read_text() == 'yes'
    assert SENTINEL not in _log(ig) and SENTINEL not in loaded.stdout and SENTINEL not in loaded.stderr
    assert 'web_exit=0' in _log(ig)


def test_overlapping_wrapper_exits_cleanly(tmp_path):
    ig = tmp_path / 'ig'
    _fake_python(tmp_path)
    env = _env(tmp_path, ig)
    env['SLEEP'] = '1'
    first = subprocess.Popen(['bash', str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(50):
        if (tmp_path / 'ready').exists():
            break
        time.sleep(0.05)
    else:
        first.kill()
        raise AssertionError('wrapper did not reach the collector')
    second = subprocess.run(['bash', str(SCRIPT)], env=env, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert second.returncode == 0
    assert 'already_running' in _log(ig)
    assert SENTINEL not in _log(ig)
    assert first.wait(timeout=10) == 0


def test_wrapper_chains_only_matching_completed_web_signals(tmp_path):
    import shutil

    scripts = tmp_path / 'checkout/scripts'
    scripts.mkdir(parents=True)
    shutil.copy2(SCRIPT, scripts / SCRIPT.name)
    publisher = scripts / 'auto_publish.sh'
    publisher.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >>"$HANSET_DATA_DIR/published"\nexit 1\n')
    publisher.chmod(0o700)
    data = tmp_path / 'data'
    ig = data / 'ig'
    env = _env(tmp_path, ig)
    env.update(HANSET_DATA_DIR=str(data), PYTHONPATH=str(REPO), HANSET_AUTO_PUBLISH='1')
    bin_path = tmp_path / 'bin'
    bin_path.mkdir()
    stub = bin_path / 'python3'
    stub.write_text(f'#!{sys.executable}\n' + '''
import json, os, sys
from pathlib import Path

if sys.argv[1] == '-':
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
if sys.argv[2] != 'hanset_active.collect.scheduled':
    sys.exit(0)
job = sys.argv[3]
root = Path(os.environ['HANSET_DATA_DIR']) / 'stamps'
root.mkdir(parents=True, exist_ok=True)
outcome = os.environ.get('TEST_OUTCOME')
token = os.environ.get('HANSET_RUN_TOKEN')
if outcome in ('completed', 'failed', 'no_change'):
    (root / (job + '.publication.json')).write_text(json.dumps(
        dict(schema_version=1, job=job, run_token=token,
             request_id=None if outcome == 'no_change' else '1' * 64)))
sys.exit(outcome == 'failed')
''')
    stub.chmod(0o700)
    for mode, outcome, enabled, catchup, expected in (
        ('wide', 'completed', '1', '', 1),
        ('wide', 'already_running', '1', '', 1),
        ('wide', 'slot_complete', '1', '', 1),
        ('wide', 'publication_readiness_required', '1', '', 1),
        ('wide', 'no_change', '1', '', 1),
        ('wide', 'failed', '1', '', 1),
        ('web', 'completed', '0', '', 1),
        ('web', 'completed', '1', 'fixture-slot', 1),
        ('ingest', 'completed', '1', '', 1),
        ('web', 'completed', '1', '', 2),
    ):
        result = subprocess.run([scripts / SCRIPT.name, mode], env={
            **env, 'TEST_OUTCOME': outcome, 'HANSET_AUTO_PUBLISH': enabled, 'HANSET_CATCHUP_SLOT': catchup,
        }, check=False)
        assert result.returncode == int(outcome == 'failed')
        assert (data / 'published').read_text().splitlines() == ['--drain'] * expected
    assert 'publication_failed' in _log(ig)
