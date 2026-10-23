"""Trusted Linux supervisor. No direct-browser-network fallback is supported."""
import base64
import json
import importlib.util
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
from .config import Rejected, REPO, policy_for
from .leases import current, process_start
from .privacy_filter import safe_url


class BrowserRunner:
    def __init__(self, worker, reservation, gate):
        self.worker, self.reservation, self.gate = worker, reservation, gate
        self.process = None
        worker.supervisor.runners[reservation['id']] = self

    def exited(self):
        return self.process is None or self.process.poll() is not None

    def stop(self):
        if self.process and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=10)

    def run(self):
        w, res = self.worker, self.reservation
        policy_for(w.cfg, res['url'], browser=True, now=w.clock())
        current(w.store.db, res, w.clock(), running=True)
        # No credentials, proxy settings, display/DBus sockets or host browser
        # profile environment are passed into the disposable process tree.
        child_env = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'LC_ALL')}
        spec = importlib.util.find_spec('playwright')
        if not spec:
            raise Rejected('browser_dependency_missing')
        child_env.update(HOME='/tmp/hanset-home', XDG_CONFIG_HOME='/tmp/hanset-config',
                         XDG_CACHE_HOME='/tmp/hanset-cache',
                         PYTHONPATH=str(Path(spec.origin).parent.parent),
                         PLAYWRIGHT_BROWSERS_PATH=os.environ.get('PLAYWRIGHT_BROWSERS_PATH',
                                                               str(Path.home() / '.cache/ms-playwright')))
        proc = subprocess.Popen(['unshare', '--user', '--map-root-user', '--net', '--mount',
                                 '--pid', '--fork', '--kill-child=KILL', '--mount-proc',
                                 sys.executable, '-m', 'crawler.browser_extract'], cwd=REPO,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                start_new_session=True, bufsize=0, close_fds=True, env=child_env)
        self.process = proc
        w.store.db.execute('INSERT OR REPLACE INTO processes VALUES (?,?,?,?,?)',
                           (res['id'], proc.pid, process_start(proc.pid), w.instance, 'running'))
        def send(value):
            proc.stdin.write((json.dumps(value) + '\n').encode())
        send({'url': res['url'], 'document_id': res['document_id']})
        deadline = time.monotonic() + min(w.cfg['lease_seconds'], max(.1, res['expires'] - w.clock()))
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        buffer = b''
        redirects = []
        try:
            while time.monotonic() < deadline:
                current(w.store.db, res, w.clock(), running=True)
                if not selector.select(min(1, max(.01, deadline - time.monotonic()))):
                    continue
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    raise Rejected('browser_process_failed')
                buffer += chunk
                if len(buffer) > 100000:
                    raise Rejected('browser_ipc_size')
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    message = json.loads(line)
                    if message['type'] == 'request':
                        try:
                            response = self.gate.request(message['url'], message['method'], message['resource_type'], browser=True)
                            if 'location' in response['headers']:
                                redirects.append({'url': response['url'], 'status': response['status'], 'to': response['headers']['location']})
                                if len(redirects) > w.cfg['max_redirects']:
                                    raise Rejected('redirect_limit')
                            send({'type': 'response', 'status': response['status'], 'headers': response['headers'],
                                  'body': base64.b64encode(response['body']).decode()})
                        except Rejected as exc:
                            w.store.db.execute('INSERT INTO audit(operation,reason,at) VALUES (?,?,?)',
                                               ('browser_request', str(exc), w.clock()))
                            if str(exc) in ('server_stop', 'blocked_page', 'permission_revoked', 'permission_check_unavailable',
                                            'request_budget_exhausted', 'total_bytes_exhausted'):
                                raise
                            send({'type': 'denied'})
                    elif message['type'] == 'result':
                        result = message['data']
                        result['final_url'] = safe_url(result['final_url'])
                        policy_for(w.cfg, result['final_url'], browser=True, now=w.clock())
                        result['redirects'] = redirects
                        proc.wait(timeout=10)
                        if proc.returncode:
                            raise Rejected('browser_process_failed')
                        return result
                    else:
                        phase = message.get('phase')
                        if phase not in ('isolation', 'driver', 'launch', 'context', 'navigation', 'extraction'):
                            phase = 'unknown'
                        raise Rejected('browser_extraction_failed_' + phase)
            raise Rejected('browser_deadline')
        finally:
            selector.close()
            self.stop()
            proc.stdin.close()
            proc.stdout.close()
            w.store.db.execute("UPDATE processes SET state='exited' WHERE reservation_id=?", (res['id'],))
