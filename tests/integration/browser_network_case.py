"""Runs only inside a disposable outer test network namespace; no Internet access.

Public-looking addresses belong to its loopback. Real DNS, pinned TLS and Chromium
are exercised; no fetch, resolver, socket, browser or policy function is mocked.
"""
import collections
import datetime as dt
import http.server
import ipaddress
import json
import os
from pathlib import Path
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time

from crawler.config import load, Rejected
from crawler.local_worker import Worker, uid
from crawler.store import Store
from crawler.network_gate import NetworkGate, public_addresses
from crawler.browser_runner import BrowserRunner
from urllib.parse import quote

PUBLIC = '93.184.216.34'
FORBIDDEN = '93.184.216.35'
counts = collections.Counter()
slow_ready = threading.Event()
slow_closed = threading.Event()


def command(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class DNS(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        index, labels = 12, []
        while data[index]:
            length = data[index]; index += 1
            labels.append(data[index:index + length].decode()); index += length
        index += 1
        qtype = struct.unpack('!H', data[index:index + 2])[0]
        question = data[12:index + 4]
        name = '.'.join(labels)
        answer = b''
        if qtype == 1:
            counts['dns:' + name] += 1
            address = PUBLIC if name != 'bad.test' else FORBIDDEN
            if name == 'rebind.test' and counts['dns:' + name] > 1:
                address = '127.0.0.1'
            answer = b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 0, 4) + socket.inet_aton(address)
        response = data[:2] + b'\x81\x80' + struct.pack('!HHHH', 1, int(bool(answer)), 0, 0) + question + answer
        sock.sendto(response, self.client_address)


class Forbidden(socketserver.BaseRequestHandler):
    def handle(self):
        counts['forbidden_connections'] += 1
        self.request.close()


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    def get_request(self):
        pair = super().get_request()
        counts['allowed_connections'] += 1
        return pair


class Content(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_GET(self):
        counts['request:' + self.path] += 1
        if self.path == '/slow':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', '100')
            self.end_headers()
            self.wfile.write(b'partial'); self.wfile.flush()
            slow_ready.set()
            self.connection.settimeout(10)
            try:
                if not self.connection.recv(1): slow_closed.set()
            except (ssl.SSLError, ConnectionError): slow_closed.set()
            return
        if self.path == '/redirect':
            self.send_response(302); self.send_header('Location', 'https://bad.test/forbidden'); self.end_headers(); return
        if self.path == '/js':
            body = '<main>Public guide</main><script>location.href="https://bad.test/forbidden";</script>'
        elif self.path == '/rebind':
            body = '<main>Public guide</main><script>fetch("https://rebind.test/again").catch(()=>{});</script>'
        elif self.path == quote('/공지사항'):
            body = '<title>Public rules</title><main>투숙객·회원 전용 / members only</main><script src="/스크립트.js"></script>'
        elif self.path == quote('/스크립트.js'):
            raw = b'globalThis.publicRulesLoaded = true;'
            self.send_response(200); self.send_header('Content-Type', 'application/javascript'); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
            return
        elif self.path == '/safe':
            body = '''<title>Public guide</title><main>Public admission <input value="never-store"><textarea>never-store</textarea></main>
<script>
for (const u of ['https://bad.test/forbidden','https://127.0.0.1/forbidden','https://[::1]/forbidden','https://[::ffff:127.0.0.1]/forbidden']) {
 fetch(u).catch(()=>{}); const s=document.createElement('script');s.src=u;document.head.append(s);
 const f=document.createElement('iframe');f.src=u;document.body.append(f);
}
window.open('https://bad.test/forbidden');
try { new WebSocket('wss://bad.test/forbidden'); } catch {}
navigator.serviceWorker.register('/sw.js').catch(()=>{});
try { const p = new RTCPeerConnection({iceServers:[{urls:'stun:127.0.0.1:443'}]});p.createDataChannel('x');p.createOffer().then(o=>p.setLocalDescription(o)); } catch {}
</script>'''
        else:
            body = '<main>Independent public guide</main>'
        raw = body.encode()
        self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)


class PacketMonitor:
    def __init__(self):
        self.stop_event = threading.Event()
        self.socket = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
        self.socket.settimeout(.1)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
    def run(self):
        while not self.stop_event.is_set():
            try: data = self.socket.recv(65536)
            except TimeoutError: continue
            except OSError: return
            proto = data[12:14]
            destination = None
            offset = 0
            if proto == b'\x08\x00' and len(data) >= 38:
                destination = socket.inet_ntop(socket.AF_INET, data[30:34])
                offset = 14 + (data[14] & 15) * 4
            elif proto == b'\x86\xdd' and len(data) >= 58:
                destination = socket.inet_ntop(socket.AF_INET6, data[38:54]); offset = 54
            if destination in (FORBIDDEN, '127.0.0.1', '::1', '::ffff:127.0.0.1') and len(data) >= offset + 4:
                port = struct.unpack('!H', data[offset + 2:offset + 4])[0]
                if port == 443: counts['forbidden_packets'] += 1
    def close(self):
        self.stop_event.set(); self.thread.join(timeout=1); self.socket.close()


def main():
    command('ip', 'link', 'set', 'lo', 'up')
    command('ip', 'addr', 'add', PUBLIC + '/32', 'dev', 'lo')
    command('ip', 'addr', 'add', FORBIDDEN + '/32', 'dev', 'lo')
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        resolv = tmp / 'resolv.conf'
        resolv.write_text('nameserver 127.0.0.53\noptions timeout:1 attempts:1\n')
        command('mount', '--bind', str(resolv), '/etc/resolv.conf')
        cert, key = tmp / 'cert.pem', tmp / 'key.pem'
        command('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key),
                '-out', str(cert), '-days', '1', '-subj', '/CN=approved.test',
                '-addext', 'subjectAltName=DNS:approved.test,DNS:rebind.test,DNS:pubmed.ncbi.nlm.nih.gov,DNS:' + '사우나.test'.encode('idna').decode())
        os.environ['SSL_CERT_FILE'] = str(cert)
        dns = socketserver.ThreadingUDPServer(('127.0.0.53', 53), DNS)
        servers = [dns]
        for address, family in ((FORBIDDEN, socket.AF_INET), ('127.0.0.1', socket.AF_INET), ('::1', socket.AF_INET6)):
            cls = type('Sink', (socketserver.ThreadingTCPServer,), {'address_family': family, 'allow_reuse_address': True, 'daemon_threads': True})
            servers.append(cls((address, 443), Forbidden))
        server = Server((PUBLIC, 443), Content)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(cert, key)
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        servers.append(server)
        for s in servers: threading.Thread(target=s.serve_forever, daemon=True).start()
        monitor = PacketMonitor()
        cfg = load()
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        cfg['policies'] = [{'origin': 'https://' + h, 'paths': ['/'], 'public': True,
                            'browser': True, 'robots_reviewed_at': today} for h in ('approved.test', 'rebind.test', '127.0.0.1', '[::1]', '[::ffff:7f00:1]', 'pubmed.ncbi.nlm.nih.gov', '사우나.test')]
        store = Store(tmp / 'state.sqlite3')
        worker = Worker(store, cfg)
        try:
            def run(url, success=True):
                res = worker.reserve(uid(), url, uid(), 'browser_extract')
                worker.begin(res)
                gate = NetworkGate(worker, res)
                try:
                    data = BrowserRunner(worker, res, gate).run()
                    if url.endswith('/공지사항'):
                        assert data['excerpt'] == '투숙객·회원 전용 / members only', 'Korean response charset lost'
                    assert success, 'denied navigation unexpectedly accepted'
                    ack = worker.accept(res, data)
                    assert 'never-store' not in json.dumps(data)
                    return ack
                except Rejected:
                    if success:
                        reasons = [r[0] for r in store.db.execute('SELECT reason FROM audit')]
                        raise AssertionError('unexpected browser failure; safe reason codes: ' + repr(reasons))
                    worker.revoke(res)
                finally:
                    gate.cancel()
            run('https://approved.test/normal')
            run('https://사우나.test/공지사항')
            assert counts['request:' + quote('/공지사항')] == 1
            assert counts['request:' + quote('/스크립트.js')] == 1
            # The HTTP path uses the same real ASCII request target and IDNA TLS.
            from crawler.fetcher import collect
            res = worker.reserve(uid(), 'https://사우나.test/공지사항', uid(), 'auto_discovery')
            worker.begin(res)
            ack = collect(worker, res)
            assert ack['record_id'] == res['record_id']
            assert counts['request:' + quote('/공지사항')] == 2
            run('https://approved.test/safe')
            run('https://approved.test/redirect', success=False)
            run('https://approved.test/js', success=False)
            run('https://rebind.test/rebind')
            assert counts['request:/again'] == 0, 'DNS rebinding reached a destination'
            assert counts['request:/sw.js'] == 0, 'service worker request escaped blocking'
            assert counts['request:/forbidden'] == 0
            assert counts['request:/safe'] == 1
            # Real browser with interception entirely absent: OS boundary still denies sockets.
            probe = r'''
import socket
from crawler.browser_extract import isolation_check
isolation_check()
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
 b=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-quic'])
 for u in ['https://93.184.216.35/forbidden','https://127.0.0.1/forbidden','https://[::1]/forbidden']:
  page=b.new_page()
  try: page.goto(u,timeout=1500)
  except Exception: pass
  page.close()
 page=b.new_page()
 page.goto('data:text/html,<main>test</main>')
 page.evaluate("""() => {fetch('https://93.184.216.35/forbidden').catch(()=>{});try{new WebSocket('wss://127.0.0.1:443')}catch{};try{const p=new RTCPeerConnection({iceServers:[{urls:'stun:127.0.0.1:443'}]});p.createDataChannel('x');p.createOffer().then(o=>p.setLocalDescription(o));}catch{}}""")
 page.wait_for_timeout(500);b.close()
for family,addr in [(socket.AF_INET,('127.0.0.1',443)),(socket.AF_INET6,('::1',443)),(socket.AF_INET,('93.184.216.35',443))]:
 with socket.socket(family) as s:
  s.settimeout(.2)
  assert s.connect_ex(addr)!=0
'''
            subprocess.run(['unshare', '--user', '--map-root-user', '--net', '--mount', '--pid', '--fork', '--kill-child=KILL', '--mount-proc',
                            sys.executable, '-c', probe], check=True, stdout=subprocess.DEVNULL, timeout=20)
            # Missing isolation fails before Chromium starts or any gate request is sent.
            result = subprocess.run([sys.executable, '-m', 'crawler.browser_extract'],
                                    input=b'{}\n', capture_output=True, timeout=10)
            assert result.returncode != 0 and b'browser_extraction_failed' in result.stdout
            # IPC/gate death tears down the actual browser rather than falling back to its network.
            res = worker.reserve(uid(), 'https://approved.test/crash', uid(), 'browser_extract')
            worker.begin(res)
            class DeadGate:
                def request(self, *a, **kw): raise Rejected('gate_unavailable')
            runner = BrowserRunner(worker, res, DeadGate())
            try: runner.run(); raise AssertionError('dead gate unexpectedly worked')
            except Rejected: pass
            assert runner.exited()
            worker.revoke(res)
            # Expiry revokes an actual in-flight HTTP/1.0 TLS connection. Its
            # response owns the socket after http.client clears conn.sock.
            shared = {}
            def slow_request():
                local = Store(tmp / 'state.sqlite3')
                w = Worker(local, cfg)
                r = w.reserve(uid(), 'https://approved.test/slow', uid())
                w.begin(r)
                gate = NetworkGate(w, r)
                shared.update(reservation=r, gate=gate)
                try:
                    gate.request(r['url'])
                    shared['unexpected_success'] = True
                except Rejected:
                    shared['rejected'] = True
                finally:
                    local.close()
            thread = threading.Thread(target=slow_request, daemon=True)
            thread.start()
            assert slow_ready.wait(15), 'slow TLS request never started'
            a = shared['reservation']
            store.db.execute('UPDATE reservations SET expires=? WHERE id=?', (time.time() - 1, a['id']))
            try:
                worker.reserve(uid(), a['url'], uid())
                raise AssertionError('elapsed lease released a live connection')
            except Rejected as exc:
                assert str(exc) == 'domain_held'
            worker.supervisor.gates[a['id']] = shared['gate']
            worker.supervisor.expire()
            thread.join(timeout=5)
            assert not thread.is_alive() and shared.get('rejected')
            assert slow_closed.wait(3), 'destination did not observe socket termination'
            assert worker.supervisor.drain(a)
            b = worker.reserve(uid(), a['url'], uid())
            assert b['generation'] == a['generation'] + 1
            worker.begin(b)
            try:
                worker.accept(a, {'document_key': a['document_id']})
                raise AssertionError('expired generation was accepted')
            except Rejected: pass
            worker.revoke(b)
            time.sleep(.2)
            assert counts['forbidden_connections'] == 0, counts
            assert counts['forbidden_packets'] == 0, counts
            assert store.db.execute('SELECT count(*) FROM observations').fetchone()[0] == 5
            print('real Chromium + pinned TLS + DNS rebinding + namespace failure + dead gate + expired socket drain: OK')
            config_file = tmp / 'test-config.json'
            config_file.write_text(json.dumps(cfg))
            subprocess.run(['xvfb-run', '-a', sys.executable, str(Path(__file__).with_name('extension_case.py')),
                            str(config_file), str(tmp)], check=True, timeout=60)
            assert counts['forbidden_connections'] == 0 and counts['forbidden_packets'] == 0, counts
            print('forbidden_connections=0 forbidden_packets=0')
        finally:
            monitor.close(); store.close()
            for s in servers: s.shutdown(); s.server_close()


if __name__ == '__main__': main()
