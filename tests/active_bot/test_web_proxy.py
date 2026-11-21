"""Collect-web proxy checks, outage recovery and direct model routing."""
import socket
import socketserver
import struct
import threading
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
import requests

from hanset_active import web_proxy
from hanset_active.collect.service import run_web
from hanset_active.config import Settings
from hanset_active.db import connect
from hanset_active.fetcher import Fetcher, FetchResult, USER_AGENT
from hanset_active.util.timeutil import FakeClock
from tests.active_bot.test_collect_web import _page, _session, _settings, _targets
from tests.active_bot.test_collect_deepseek import enable_qwen


PROXY = 'socks5h://127.0.0.1:1080'
PRIVATE = 'SYNTHETIC_PRIVATE_ARGUMENT'


class RetryClock(FakeClock):
    def __init__(self):
        super().__init__()
        self.waits = []

    def sleep(self, seconds):
        self.waits.append(seconds)
        super().sleep(seconds)


def http_response(status, body=b'', **headers):
    return SimpleNamespace(status_code=status, headers={'Content-Type': 'text/html', **headers},
                           iter_content=lambda size: iter([body]), close=lambda: None)


@pytest.fixture
def socks():
    """Minimal no-auth SOCKS5 relay on loopback that records destinations."""
    seen = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            conn = self.request
            n = conn.recv(2)[1]
            conn.recv(n)
            conn.sendall(b'\x05\x00')
            _, _, _, atyp = conn.recv(4)
            if atyp == 3:
                host = conn.recv(conn.recv(1)[0]).decode()
            else:
                host = socket.inet_ntoa(conn.recv(4))
            port = struct.unpack('>H', conn.recv(2))[0]
            seen.append((host, port))
            upstream = socket.create_connection((host, port))
            conn.sendall(b'\x05\x00\x00\x01' + socket.inet_aton('127.0.0.1') + b'\x00\x00')

            def pump(a, b):
                try:
                    while True:
                        data = a.recv(65536)
                        if not data:
                            break
                        b.sendall(data)
                except OSError:
                    pass
                finally:
                    try:
                        b.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
            t = threading.Thread(target=pump, args=(upstream, conn), daemon=True)
            t.start()
            pump(conn, upstream)
            t.join(5)
            upstream.close()

    srv = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
    srv.daemon_threads = True
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'socks5h://127.0.0.1:{srv.server_address[1]}', seen
    finally:
        srv.shutdown(); srv.server_close()


def _fetcher(tmp_path, proxy, *, public_scope=False):
    settings = Settings(data_dir=tmp_path / 'external', testing=True, allow_private_hosts=True, min_interval=0, global_gap=0)
    return Fetcher(settings, connect(settings.data_dir / 'test.sqlite3'), FakeClock(),
                   proxy=proxy, public_scope=public_scope)


@pytest.mark.parametrize('value', ['http://127.0.0.1:1080', 'socks5://10.0.0.1:1080',
                                   'socks5://user:pw@127.0.0.1:1080', 'socks5://127.0.0.1'])
def test_proxy_must_be_loopback_socks5(value):
    with pytest.raises(ValueError):
        web_proxy.configured({web_proxy.ENV: value})


def test_unset_proxy_keeps_fetcher_direct(tmp_path):
    assert web_proxy.configured({}) == ''
    fetcher = _fetcher(tmp_path, '')
    assert fetcher.proxy == '' and not fetcher.session.proxies


def test_fetcher_uses_proxy_and_skips_heavy_resources(server, socks, tmp_path):
    proxy, seen = socks
    server.add('/page', _page())
    server.add('/logo.png', b'\x89PNG' + b'0' * 5000, headers={'Content-Type': 'image/png'})
    fetcher = _fetcher(tmp_path, proxy)
    assert fetcher.fetch(server.base + '/page').ok
    image = fetcher.fetch(server.base + '/logo.png')
    assert not image.ok and image.reason == 'heavy resource' and image.content == b''
    assert seen and all(host == '127.0.0.1' for host, _ in seen)


def test_fetcher_continues_direct_after_proxy_outage(tmp_path, monkeypatch):
    fetcher = _fetcher(tmp_path, PROXY, public_scope=True)
    failed, next_url = 'https://spa.example.test/first', 'https://spa.example.test/next'
    robots = 'https://spa.example.test/robots.txt'
    calls = []

    def get(self, url, **kwargs):
        calls.append((url, bool(self.proxies)))
        if self.proxies:
            raise requests.ConnectionError
        return http_response(404) if url == robots else http_response(200, _page().encode())

    monkeypatch.setattr(requests.Session, 'get', get)
    monkeypatch.setattr(web_proxy, 'reachable', lambda proxy: False)
    result = fetcher.fetch(failed)
    assert (result.outcome, result.reason) == ('error', 'proxy_unavailable')
    assert fetcher.proxy_failed
    assert fetcher.db.execute('SELECT COUNT(*) FROM robots_cache').fetchone()[0] == 0
    assert fetcher.fetch(next_url).ok
    assert calls == [(robots, True), (robots, False), (next_url, False)]


def test_run_web_continues_direct_when_proxy_down(tmp_path, monkeypatch):
    enable_qwen(monkeypatch)
    monkeypatch.setenv(web_proxy.ENV, PROXY)
    monkeypatch.delenv(web_proxy.HEAL_ENV, raising=False)
    probe = Mock(return_value=False)
    monkeypatch.setattr(web_proxy, 'reachable', probe)
    calls = []

    def get(self, url, **kwargs):
        calls.append(url)
        assert not self.proxies and not self.trust_env and self.headers['User-Agent'] == USER_AGENT
        return http_response(404) if url.endswith('/robots.txt') else http_response(200, _page().encode())

    monkeypatch.setattr(requests.Session, 'get', get)
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    url = 'https://spa.example.test/hours'
    _targets(settings.data_dir / 'web_targets.json', url, name='Seed Spa A')
    session = _session()
    result = run_web(settings, clock=FakeClock(), qwen_session=session, testing=True)
    assert result['state'] == 'done' and result['success'] and result['collected']
    assert result['proxy_heal'] == {**web_proxy.new_heal_status(), 'retries': 2}
    assert result['web_egress'] == {'mode': 'direct', 'reason': 'proxy_unavailable', 'phase': 'preflight'}
    assert probe.call_count == 3 and calls == [url]
    assert session.calls and 'proxies' not in session.calls[0]


@pytest.mark.parametrize('status,body', [
    (403, b''), (402, b''), (200, b'<main>Verify you are human</main>'),
    (200, b'<main><input type="password"></main>'),
    (200, b'<main>Subscribe to continue reading</main>'),
])
def test_healthy_proxy_site_refusal_does_not_change_egress(tmp_path, monkeypatch, status, body):
    enable_qwen(monkeypatch)
    monkeypatch.setenv(web_proxy.ENV, PROXY)
    probe = Mock(return_value='')
    monkeypatch.setattr(web_proxy, 'preflight', probe)
    monkeypatch.setattr(web_proxy, 'reachable', Mock(side_effect=AssertionError('unexpected outage probe')))
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    url = 'https://spa.example.test/hours'
    _targets(settings.data_dir / 'web_targets.json', url, name='Seed Spa A')
    calls = []

    def get(self, url, **kwargs):
        calls.append(url)
        assert self.proxies == {'http': PROXY, 'https': PROXY}
        return http_response(404) if url.endswith('/robots.txt') else http_response(status, body)

    monkeypatch.setattr(requests.Session, 'get', get)
    model = _session()
    result = run_web(settings, clock=FakeClock(), qwen_session=model, testing=True)
    assert result['success'] and 'web_egress' not in result and not model.calls
    assert result['pages'][-1]['disposition'] in ('blocked', 'login_wall')
    assert calls == [url]


def test_mid_run_outage_preserves_blocks_and_retries_direct(tmp_path, monkeypatch):
    from contextlib import closing

    from hanset_active.collect.store import connect as connect_receipts, digest
    from hanset_active.collect.state import State

    enable_qwen(monkeypatch)
    monkeypatch.setenv(web_proxy.ENV, PROXY)
    monkeypatch.setattr(web_proxy, 'preflight', lambda *args, **kwargs: '')
    monkeypatch.setattr(web_proxy, 'reachable', lambda proxy: False)
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    blocked, collateral = ('https://spa.example.test/' + path for path in ('blocked', 'collateral'))
    failed, next_url = ('https://box.test/' + path for path in ('outage', 'next'))
    _targets(settings.data_dir / 'web_targets.json', blocked, name='Seed Spa A',
             official_urls=[blocked, failed, collateral, next_url])
    calls = []

    def get(self, url, **kwargs):
        calls.append((url, bool(self.proxies)))
        if url.endswith('/robots.txt'):
            return http_response(404)
        if url == blocked:
            return http_response(403)
        if url == failed and self.proxies:
            raise requests.ConnectionError
        if url == failed:
            return http_response(404)
        assert url == next_url
        return http_response(200, _page().encode())

    monkeypatch.setattr(requests.Session, 'get', get)
    model, clock = _session(), FakeClock()
    result = run_web(settings, clock=clock, qwen_session=model, testing=True)
    assert result['success'] and result['collected'] and result['counts']['pages'] == 4
    assert result['web_egress'] == {'mode': 'direct', 'reason': 'proxy_unavailable', 'phase': 'mid_run'}
    assert calls == [(blocked, True), (failed, True), (failed, False), (next_url, False)]
    assert failed not in State(settings.data_dir, clock).data['completed']
    with closing(connect_receipts(settings.data_dir)) as receipts:
        assert receipts.execute('SELECT disposition FROM web_target_observations WHERE url_hash=?',
                                (digest(failed),)).fetchone()[0] == 'error'
        assert receipts.execute('SELECT count(*) FROM web_target_blocks').fetchone()[0] == 1


def test_run_web_through_proxy_keeps_model_direct(server, socks, tmp_path, monkeypatch):
    proxy, seen = socks
    enable_qwen(monkeypatch)
    monkeypatch.setenv(web_proxy.ENV, proxy)
    server.add('/hours', _page())
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    _targets(settings.data_dir / 'web_targets.json', server.base + '/hours')
    session = _session()
    result = run_web(settings, clock=FakeClock(), qwen_session=session, testing=True)
    assert result['success'] is True and result['collected'] is True
    assert seen  # page traffic crossed the proxy
    assert session.calls and 'proxies' not in session.calls[0]  # model session untouched
    assert getattr(session, 'proxies', {}) in ({}, None)


@pytest.mark.parametrize('heal,success_at', [
    (True, 0), (True, 1), (True, 2), (True, 3),
    (False, 0), (False, 1), (False, 2),
])
def test_retry_stops_at_first_healthy_probe(monkeypatch, heal, success_at):
    env = {web_proxy.HEAL_ENV: "heal-example 'two words'"} if heal else {}
    probe = Mock(side_effect=['proxy_unavailable'] * success_at + [''])
    hook = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(web_proxy, 'preflight', probe)
    monkeypatch.setattr(web_proxy.subprocess, 'run', hook)
    clock = RetryClock()
    reason, status = web_proxy.preflight_with_retry(PROXY, environ=env, clock=clock)
    assert reason == ''
    assert probe.call_args_list == [call(PROXY, environ=env)] * (success_at + 1)
    assert clock.waits == (([15, 30, 45] if heal else [2, 4])[:success_at])
    assert clock.elapsed == sum(clock.waits)
    assert status == {'attempted': heal and success_at > 0,
                      'heal_exit': 0 if heal and success_at > 0 else None,
                      'retries': success_at, 'recovered': success_at > 0}
    if heal and success_at:
        hook.assert_called_once_with(
            ['heal-example', 'two words'], shell=False, timeout=30, check=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        hook.assert_not_called()


@pytest.mark.parametrize('command', [None, '', ' \t\n'])
def test_retry_without_hook_exhausted(monkeypatch, command):
    env = {} if command is None else {web_proxy.HEAL_ENV: command}
    probe = Mock(return_value='proxy_unavailable')
    hook = Mock()
    monkeypatch.setattr(web_proxy, 'preflight', probe)
    monkeypatch.setattr(web_proxy.subprocess, 'run', hook)
    clock = RetryClock()
    reason, status = web_proxy.preflight_with_retry(PROXY, environ=env, clock=clock)
    assert reason == 'proxy_unavailable'
    assert probe.call_count == 3 and clock.waits == [2, 4]
    assert status == {**web_proxy.new_heal_status(), 'retries': 2}
    hook.assert_not_called()


@pytest.mark.parametrize('failure,exit_code,launched', [
    ('timeout', None, True), ('launch', None, True), ('quoting', None, False),
    ('nul', None, True), ('empty', None, False), ('nonzero', 7, True), ('zero', 0, True),
])
def test_hook_failure_is_quiet_and_never_authorizes_fetch(
        monkeypatch, capsys, caplog, failure, exit_code, launched):
    command = 'heal-example ' + PRIVATE
    if failure == 'quoting':
        command += " '"
    elif failure == 'nul':
        command += '\0'
    hook = Mock(return_value=SimpleNamespace(returncode=exit_code))
    if failure == 'timeout':
        hook.side_effect = subprocess.TimeoutExpired(command, 30, output=PRIVATE, stderr=PRIVATE)
    elif failure == 'launch':
        hook.side_effect = OSError(PRIVATE)
    elif failure == 'nul':
        hook.side_effect = ValueError(PRIVATE)
    if failure == 'empty':
        monkeypatch.setattr(web_proxy.shlex, 'split', Mock(return_value=[]))
    monkeypatch.setattr(web_proxy.subprocess, 'run', hook)
    probe = Mock(return_value='proxy_unavailable')
    monkeypatch.setattr(web_proxy, 'preflight', probe)
    clock = RetryClock()
    reason, status = web_proxy.preflight_with_retry(
        PROXY, environ={web_proxy.HEAL_ENV: command}, clock=clock)
    assert reason == 'proxy_unavailable'
    assert status == {'attempted': True, 'heal_exit': exit_code, 'retries': 3, 'recovered': False}
    assert probe.call_count == 4 and clock.waits == [15, 30, 45]
    assert hook.call_count == int(launched)
    output = capsys.readouterr()
    assert PRIVATE not in output.out + output.err + caplog.text + json.dumps(status)
    if launched:
        assert hook.call_args.kwargs == {
            'shell': False, 'timeout': 30, 'check': False,
            'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}


@pytest.mark.parametrize('final', ['proxy_unavailable', 'proxy_exit_mismatch'])
def test_retry_preserves_final_reason(monkeypatch, final):
    probe = Mock(side_effect=['proxy_exit_mismatch', 'proxy_exit_mismatch', final])
    monkeypatch.setattr(web_proxy, 'preflight', probe)
    reason, status = web_proxy.preflight_with_retry(PROXY, environ={}, clock=RetryClock())
    assert reason == final and status['retries'] == 2 and not status['recovered']


@pytest.fixture
def mocked_web(tmp_path, monkeypatch):
    """No sockets, models, subprocesses or real sleeps in recovery integration."""
    from hanset_active.collect import service
    enable_qwen(monkeypatch)
    monkeypatch.setenv(web_proxy.ENV, PROXY)
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    service.State(settings.data_dir, FakeClock()).save()
    targets = settings.data_dir / 'web_targets.json'
    _targets(targets, 'https://venue.example/hours')
    monkeypatch.setattr(service, '_change_cycle', Mock(return_value={'reason': 'no_events'}))
    fetcher = Mock(proxy_failed=False)
    fetcher.fetch.return_value = FetchResult('https://venue.example/hours', 'ok', status=200)
    constructor = Mock(return_value=fetcher)
    monkeypatch.setattr(service, 'Fetcher', constructor)
    monkeypatch.setattr(service, 'classify_page', Mock(return_value=('ready', '운영시간 06:00-22:00')))
    monkeypatch.setattr(service, 'inspect_page', Mock(return_value=[]))
    model = _session()
    monkeypatch.setattr('hanset_active.collect.deepseek.new_qwen_session', lambda: model)
    hook = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(web_proxy.subprocess, 'run', hook)
    return SimpleNamespace(settings=settings, targets=targets, clock=RetryClock(),
                           constructor=constructor, fetcher=fetcher, hook=hook, service=service, model=model)


@pytest.mark.parametrize('variant,reason,retries,attempted,exit_code', [
    ('no_hook', 'proxy_unavailable', 2, False, None),
    ('timeout', 'proxy_unavailable', 3, True, None),
    ('launch', 'proxy_unavailable', 3, True, None),
    ('quoting', 'proxy_unavailable', 3, True, None),
    ('nonzero', 'proxy_unavailable', 3, True, 7),
    ('zero', 'proxy_unavailable', 3, True, 0),
    ('mismatch', 'proxy_exit_mismatch', 3, True, 0),
    ('invalid', 'proxy_invalid', 0, False, None),
])
def test_web_retry_egress_and_status(
        mocked_web, monkeypatch, capsys, caplog, variant, reason, retries, attempted, exit_code):
    web = mocked_web
    if variant != 'no_hook':
        monkeypatch.setenv(web_proxy.HEAL_ENV, 'heal-example ' + PRIVATE)
    if variant == 'timeout':
        web.hook.side_effect = subprocess.TimeoutExpired(PRIVATE, 30, output=PRIVATE, stderr=PRIVATE)
    elif variant == 'launch':
        web.hook.side_effect = OSError(PRIVATE)
    elif variant == 'quoting':
        monkeypatch.setenv(web_proxy.HEAL_ENV, "heal-example '" + PRIVATE)
    elif variant == 'nonzero':
        web.hook.return_value = SimpleNamespace(returncode=7)
    if variant == 'invalid':
        monkeypatch.setenv(web_proxy.ENV, 'https://127.0.0.1:1080')
    probe = Mock(return_value='proxy_exit_mismatch' if variant == 'mismatch' else 'proxy_unavailable')
    monkeypatch.setattr(web_proxy, 'preflight', probe)
    result = run_web(web.settings, targets_path=web.targets, clock=web.clock, testing=True)
    expected = {'attempted': attempted, 'heal_exit': exit_code, 'retries': retries, 'recovered': False}
    if reason == 'proxy_unavailable':
        assert result['state'] == 'done' and result['success'] and result['collected']
        assert result['web_egress'] == {'mode': 'direct', 'reason': reason, 'phase': 'preflight'}
        assert web.constructor.call_args.kwargs['proxy'] == ''
    else:
        assert result['state'] == 'stopped' and result['reason'] == reason
        assert not result['success'] and not result['collected'] and 'web_egress' not in result
        web.constructor.assert_not_called()
    assert result['proxy_heal'] == expected
    assert probe.call_count == (0 if variant == 'invalid' else retries + 1)
    assert web.clock.waits == ([15, 30, 45] if attempted else [2, 4] if retries else [])
    assert web.hook.call_count == int(attempted and variant != 'quoting')
    persisted = json.loads((web.settings.data_dir / 'status.json').read_text())
    assert persisted['proxy_heal'] == expected
    assert web.service.run_status(web.settings)['proxy_heal'] == expected
    assert web.service.run_status(web.settings).get('web_egress') == result.get('web_egress')
    output = capsys.readouterr()
    assert PRIVATE not in json.dumps(result) + json.dumps(persisted) + output.out + output.err + caplog.text


@pytest.mark.parametrize('heal', [False, True])
@pytest.mark.parametrize('outcome', ['done', 'deferred', 'partial_model', 'mid_run_outage'])
def test_recovery_status_survives_writers_and_cli(mocked_web, monkeypatch, capsys, outcome, heal):
    from hanset_active.cli import main
    from hanset_active.collect.state import State
    from hanset_active.locks import AlreadyRunning
    web = mocked_web
    if heal:
        monkeypatch.setenv(web_proxy.HEAL_ENV, 'heal-example')
    probe = Mock(side_effect=['proxy_unavailable', ''])
    monkeypatch.setattr(web_proxy, 'preflight', probe)
    expected = {'attempted': heal, 'heal_exit': 0 if heal else None, 'retries': 1, 'recovered': True}
    written = []
    original_write = State.write_status

    def capture(state, result):
        original_write(state, result)
        written.append(json.loads((state.directory / 'status.json').read_text())['proxy_heal'])

    monkeypatch.setattr(State, 'write_status', capture)
    if outcome == 'deferred':
        lock = Mock()
        lock.__enter__ = Mock(side_effect=AlreadyRunning())
        lock.__exit__ = Mock()
        monkeypatch.setattr(web.service, 'RunLock', Mock(return_value=lock))
    elif outcome == 'partial_model':
        web.model.error = requests.Timeout('fixture')
    elif outcome == 'mid_run_outage':
        web.fetcher.fetch.return_value = FetchResult('https://venue.example/hours', 'error', reason='proxy_unavailable')
        web.service.classify_page.return_value = ('error', None)
    result = run_web(web.settings, targets_path=web.targets, clock=web.clock, testing=True)
    assert result['state'] == {'partial_model': 'partial', 'mid_run_outage': 'done'}.get(outcome, outcome)
    assert result['proxy_heal'] == expected and written == [expected]
    assert web.clock.waits == ([15] if heal else [2])
    assert web.hook.call_count == int(heal)
    assert json.loads((web.settings.data_dir / 'status.json').read_text())['proxy_heal'] == expected
    assert web.service.run_status(web.settings)['proxy_heal'] == expected
    assert 'proxy_heal' not in json.loads((web.settings.data_dir / 'checkpoint.json').read_text())
    if outcome == 'deferred':
        web.constructor.assert_not_called()
    else:
        assert web.constructor.call_args.kwargs['proxy'] == PROXY
    monkeypatch.setenv('HANSET_IG_DIR', str(web.settings.data_dir))
    assert main(['collect', 'status']) == (0 if outcome in ('done', 'mid_run_outage') else 2)
    status = json.loads(capsys.readouterr().out)
    assert status['proxy_heal'] == expected
    if outcome == 'mid_run_outage':
        assert status['web_egress'] == {'mode': 'direct', 'reason': 'proxy_unavailable', 'phase': 'mid_run'}


def test_public_heal_status_strict_types_and_legacy_files(tmp_path):
    from hanset_active.collect.service import public_result, run_status
    from hanset_active.collect.state import State
    base = {'state': 'done', 'reason': 'ok', 'success': True, 'collected': False, 'mode': 'web'}
    valid = {'attempted': True, 'heal_exit': -15, 'retries': 3, 'recovered': False}
    assert public_result({**base, 'proxy_heal': valid})['proxy_heal'] == valid
    assert public_result({**base, 'proxy_heal': valid})['proxy_heal'] is not valid
    bad = [None, [], {}, {**valid, 'extra': PRIVATE}]
    for key, values in {'attempted': [0, 'true', None], 'heal_exit': [True, '0', 0.0],
                        'retries': [True, -1, 4, 1.0, '1'], 'recovered': [1, 'false', None]}.items():
        bad.extend({**valid, key: value} for value in values)
    bad.extend({key: value for key, value in valid.items() if key != missing} for missing in valid)
    for value in bad:
        assert 'proxy_heal' not in public_result({**base, 'proxy_heal': value})
    settings = _settings(tmp_path)
    state = State(settings.data_dir, FakeClock())
    state.write_status(base)
    assert 'proxy_heal' not in run_status(settings)
    assert 'proxy_heal' not in public_result({**base, 'mode': 'instagram'})


def test_web_defaults_do_not_reuse_previous_heal(mocked_web, monkeypatch):
    web = mocked_web
    monkeypatch.setattr(web_proxy, 'preflight', Mock(side_effect=['proxy_unavailable', '']))
    first = run_web(web.settings, targets_path=web.targets, clock=web.clock, testing=True)
    assert first['proxy_heal']['recovered'] is True
    web.service.State(web.settings.data_dir, web.clock).complete('https://venue.example/hours', 'applied')
    second = run_web(web.settings, targets_path=web.targets, clock=web.clock, testing=True)
    assert second['reason'] == 'already_complete'
    assert second['proxy_heal'] == web_proxy.new_heal_status()


@pytest.mark.parametrize('expected,observed,reason', [
    ('', '', ''), ('192.0.2.1', '192.0.2.1', ''),
    ('192.0.2.1', '192.0.2.2', 'proxy_exit_mismatch'),
])
def test_preflight_connectivity_check_requires_expected_exit(monkeypatch, expected, observed, reason):
    monkeypatch.setattr(web_proxy, 'reachable', Mock(return_value=True))
    session = Mock()
    session.get.return_value = SimpleNamespace(status_code=200, text=observed)
    result = web_proxy.preflight(PROXY, environ={web_proxy.EXIT_ENV: expected}, session=session)
    assert result == reason
    if expected:
        assert session.trust_env is False
        session.get.assert_called_once_with(
            web_proxy.DEFAULT_CHECK_URL, proxies={'http': PROXY, 'https': PROXY}, timeout=10.0,
            headers={'User-Agent': USER_AGENT}, allow_redirects=False)
    else:
        session.get.assert_not_called()


@pytest.mark.parametrize('env', [
    {web_proxy.EXIT_ENV: 'malformed'},
    {web_proxy.EXIT_ENV: '192.0.2.1', web_proxy.CHECK_ENV: 'https://instagr.am/seed'},
    {web_proxy.EXIT_ENV: '192.0.2.1', web_proxy.CHECK_ENV: 'https://spa.example.test:invalid'},
])
def test_invalid_exit_check_config_skips_probes_and_healing(monkeypatch, env):
    reach = Mock()
    hook = Mock()
    monkeypatch.setattr(web_proxy, 'reachable', reach)
    monkeypatch.setattr(web_proxy.subprocess, 'run', hook)
    reason, status = web_proxy.preflight_with_retry(PROXY, environ={**env, web_proxy.HEAL_ENV: 'heal-example'},
                                                   clock=FakeClock())
    assert reason == 'proxy_invalid' and status == web_proxy.new_heal_status()
    reach.assert_not_called()
    hook.assert_not_called()
