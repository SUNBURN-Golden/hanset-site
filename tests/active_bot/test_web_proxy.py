"""collect-web egress proxy: fail closed, heavy resources skipped, model calls direct."""
import socket
import socketserver
import struct
import threading

import pytest

from hanset_active import web_proxy
from hanset_active.collect.service import run_web
from hanset_active.config import Settings
from hanset_active.db import connect
from hanset_active.fetcher import Fetcher
from hanset_active.util.timeutil import FakeClock
from tests.active_bot.test_collect_web import _page, _session, _settings, _targets
from tests.active_bot.test_collect_deepseek import KEY


def _unused_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


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


def _fetcher(tmp_path, proxy):
    settings = Settings(data_dir=tmp_path / 'external', testing=True, allow_private_hosts=True, min_interval=0, global_gap=0)
    return Fetcher(settings, connect(settings.data_dir / 'test.sqlite3'), FakeClock(), proxy=proxy)


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


def test_fetcher_fails_closed_when_proxy_down(server, tmp_path):
    server.add('/page', _page())
    fetcher = _fetcher(tmp_path, f'socks5h://127.0.0.1:{_unused_port()}')
    with pytest.raises(web_proxy.ProxyUnavailable):
        fetcher.fetch(server.base + '/page')
    assert fetcher.proxy_failed
    assert server.log == []  # no direct fallback
    assert fetcher.db.execute('SELECT COUNT(*) FROM robots_cache').fetchone()[0] == 0


def test_run_web_skips_fetches_when_proxy_down(server, tmp_path, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    monkeypatch.setenv(web_proxy.ENV, f'socks5h://127.0.0.1:{_unused_port()}')
    server.add('/hours', _page())
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    _targets(settings.data_dir / 'web_targets.json', server.base + '/hours')
    session = _session()
    result = run_web(settings, clock=FakeClock(), session=session, testing=True)
    assert result['state'] == 'stopped' and result['reason'] == 'proxy_unavailable'
    assert result['success'] is False and server.log == [] and not session.calls


def test_run_web_through_proxy_keeps_model_direct(server, socks, tmp_path, monkeypatch):
    proxy, seen = socks
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    monkeypatch.setenv(web_proxy.ENV, proxy)
    server.add('/hours', _page())
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    _targets(settings.data_dir / 'web_targets.json', server.base + '/hours')
    session = _session()
    result = run_web(settings, clock=FakeClock(), session=session, testing=True)
    assert result['success'] is True and result['collected'] is True
    assert seen  # page traffic crossed the proxy
    assert session.calls and 'proxies' not in session.calls[0]  # model session untouched
    assert getattr(session, 'proxies', {}) in ({}, None)
