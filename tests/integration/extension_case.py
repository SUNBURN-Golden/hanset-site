"""Real headed Chromium extension lifecycle tests, invoked in the isolated test LAN.

Xvfb/xdotool drive the browser's actual optional-host permission prompt.
"""
import copy
import http.server
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from playwright.sync_api import sync_playwright
from crawler.config import load, REPO
from crawler.intake import handler
from crawler.local_worker import Worker
from crawler.store import Store


def run(config_path, temp):
    cfg = load(config_path)
    state = Path(temp) / 'extension.sqlite3'
    control = {'drop_result': False, 'drop_begin': False, 'stop': False}
    ready = threading.Event()
    def server_thread():
        store = Store(state)
        worker = Worker(store, cfg)
        base = handler(worker)
        class FaultIntake(base):
            def reply(self, status, value):
                flag = 'drop_result' if self.path == '/result' else 'drop_begin' if self.path == '/begin' else None
                if flag and control[flag] and status == 200:
                    control[flag] = False
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                super().reply(status, value)
        server = http.server.HTTPServer(('127.0.0.1', 8766), FaultIntake)
        server.timeout = .1
        ready.set()
        try:
            while not control['stop']:
                server.handle_request(); worker.supervisor.expire()
        finally: server.server_close(); store.close()

    def stats():
        store = Store(state, readonly=True)
        try:
            return (store.db.execute('SELECT COALESCE(sum(attempts),0) FROM counters').fetchone()[0],
                    store.db.execute('SELECT count(*) FROM observations').fetchone()[0])
        finally: store.close()

    with sync_playwright() as p:
        ext = str(REPO / 'tools/chrome-clipper')
        context = p.chromium.launch_persistent_context(str(Path(temp) / 'extension-profile'), channel='chromium',
                    headless=False, ignore_https_errors=True, args=['--no-sandbox', '--no-proxy-server',
                    '--disable-extensions-except=' + ext, '--load-extension=' + ext])
        worker = context.service_workers[0] if context.service_workers else context.wait_for_event('serviceworker')
        extension_origin = worker.url.rsplit('/', 1)[0]
        cfg['extension_origins'] = [extension_origin]
        popup = context.new_page(); popup.goto(extension_origin + '/popup.html')
        popup.select_option('#origin', 'https://pubmed.ncbi.nlm.nih.gov')
        # Denying the actual permission dialog must stop before any intake job.
        popup.click('#approve')
        popup.wait_for_timeout(1500)
        subprocess.run(['xdotool', 'key', 'Return'], check=True)
        popup.wait_for_timeout(700)
        assert not popup.evaluate("chrome.permissions.contains({origins:['http://127.0.0.1/*']})")
        assert not popup.evaluate("chrome.storage.local.get('jobs').then(o=>o.jobs)")
        assert '거절' in popup.locator('#status').inner_text()
        popup.click('#approve')
        # Chromium temporarily disables Allow to prevent accidental prompt clicks.
        popup.wait_for_timeout(1500)
        subprocess.run(['xdotool', 'key', 'Tab', 'Return'], check=True)
        popup.wait_for_timeout(700)
        assert worker.evaluate("chrome.permissions.contains({origins:['http://127.0.0.1/*','https://pubmed.ncbi.nlm.nih.gov/*']})")
        page = context.new_page()
        page.goto('https://pubmed.ncbi.nlm.nih.gov/manual-one')
        page.bring_to_front()
        # Offline: a real scripting API reads URL metadata only, never the region.
        job = worker.evaluate("handle({action:'clip'})")
        assert job['state'] == 'intent' and 'data' not in job
        assert all('excerpt' not in json.dumps(j) for j in worker.evaluate('jobs()').values())
        thread = threading.Thread(target=server_thread, daemon=True); thread.start()
        assert ready.wait(5)
        # Commit succeeds, response is lost: the extension retains already sanitized data.
        control['drop_result'] = True
        job = worker.evaluate("handle({action:'clip'})")
        assert job['state'] == 'ready', job
        assert stats() == (1, 1)
        assert 'never-store' not in json.dumps(worker.evaluate('jobs()'))
        cdp = context.browser.new_browser_cdp_session()
        def stop_extension_worker():
            targets = cdp.send('Target.getTargets')['targetInfos']
            target = next(t for t in targets if t['type'] == 'service_worker' and t['url'].startswith(extension_origin))
            assert cdp.send('Target.closeTarget', {'targetId': target['targetId']})['success']
            for _ in range(50):
                if target['targetId'] not in {t['targetId'] for t in cdp.send('Target.getTargets')['targetInfos']}:
                    return
                popup.wait_for_timeout(100)
            raise AssertionError('extension service-worker target did not terminate')
        stop_extension_worker()
        popup.evaluate("chrome.runtime.sendMessage({action:'resume'})")
        assert stats() == (1, 1), 'restart must not reserve or extract again'
        assert all(j['state'] == 'acked' for j in popup.evaluate("chrome.storage.local.get('jobs').then(o=>o.jobs)").values())
        # Lost begin response leaves no extracted payload, including after real termination.
        page.goto('https://pubmed.ncbi.nlm.nih.gov/manual-two'); page.bring_to_front()
        control['drop_begin'] = True
        job = popup.evaluate("chrome.runtime.sendMessage({action:'clip'})")
        assert job['state'] == 'uncertain'
        assert all('data' not in j for j in popup.evaluate("chrome.storage.local.get('jobs').then(o=>o.jobs)").values())
        stop_extension_worker()
        popup.evaluate("chrome.runtime.sendMessage({action:'resume'})")
        assert stats() == (2, 1)
        # Real host permission withdrawal triggers the durable grant revocation path.
        approved = popup.evaluate("chrome.runtime.sendMessage({action:'approve',origin:'https://pubmed.ncbi.nlm.nih.gov'})")
        assert approved['ok']
        # A separate Linux worker must obtain fresh, actual Chrome permission
        # responses before starting, and before the real gated browser request.
        def run_once():
            outcomes = []
            def execute():
                local = Store(state)
                try: outcomes.append(Worker(local, cfg).once())
                finally: local.close()
            thread = threading.Thread(target=execute, daemon=True)
            thread.start()
            while thread.is_alive():
                popup.wait_for_timeout(100)
            thread.join()
            assert len(outcomes) == 1
            return outcomes[0]
        page.goto('https://pubmed.ncbi.nlm.nih.gov/unattended'); page.bring_to_front()
        submitted = popup.evaluate("chrome.runtime.sendMessage({action:'submit'})")
        assert submitted['ok']
        assert run_once()
        assert stats() == (3, 2)
        page.goto('https://pubmed.ncbi.nlm.nih.gov/unattended-paused'); page.bring_to_front()
        assert popup.evaluate("chrome.runtime.sendMessage({action:'submit'})")['ok']
        stop_extension_worker()
        assert not run_once(), 'terminated permission responder must prevent browser start'
        assert stats() == (3, 2), 'offline consent must not reserve a body slot'
        popup.evaluate("chrome.runtime.sendMessage({action:'resume'})")
        assert popup.evaluate("chrome.permissions.remove({origins:['https://pubmed.ncbi.nlm.nih.gov/*']})")
        popup.wait_for_timeout(500)
        assert not popup.evaluate("chrome.permissions.contains({origins:['https://pubmed.ncbi.nlm.nih.gov/*']})")
        denied = popup.evaluate("chrome.runtime.sendMessage({action:'submit'})")
        assert not denied['ok']
        store = Store(state, readonly=True)
        try:
            assert store.db.execute('SELECT enabled FROM grants').fetchone()[0] == 0
        finally: store.close()
        context.close()
        control['stop'] = True; thread.join(timeout=5)
    print('real Chrome extension: offline, ACK loss, service-worker termination, begin loss, permission withdrawal OK')


if __name__ == '__main__': run(sys.argv[1], sys.argv[2])
