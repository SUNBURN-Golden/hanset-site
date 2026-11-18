"""Real headed Chromium extension lifecycle tests, invoked in the isolated test LAN.

Xvfb/xdotool drive the browser's actual optional-host permission prompt.
"""
import http.server
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from playwright.sync_api import sync_playwright
from crawler.config import load, REPO
from crawler.intake import handler
from crawler.local_worker import Worker
from crawler.store import Store


def run(config_path, temp):
    cfg = load(config_path)
    source_origin = 'https://pubmed.ncbi.nlm.nih.gov'
    guide_base = source_origin + '/guides/'
    grant_key = 'grant:' + source_origin
    cases = [case for case in json.loads((REPO / 'tests/fixtures/minimal_regions.json').read_text())
             if not case.get('http_only')]
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
        popup.select_option('#origin', source_origin)
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
        page.goto(guide_base + 'manual-one')
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
        page.goto(guide_base + 'manual-two'); page.bring_to_front()
        control['drop_begin'] = True
        job = popup.evaluate("chrome.runtime.sendMessage({action:'clip'})")
        assert job['state'] == 'uncertain'
        assert all('data' not in j for j in popup.evaluate("chrome.storage.local.get('jobs').then(o=>o.jobs)").values())
        stop_extension_worker()
        popup.evaluate("chrome.runtime.sendMessage({action:'resume'})")
        assert stats() == (2, 1)
        # Chrome consent can be registered for an origin with only guide access.
        approved = popup.evaluate("origin => chrome.runtime.sendMessage({action:'approve',origin})", source_origin)
        assert approved['ok']
        grant_marker = 'key => chrome.storage.local.get(key).then(o => o[key])'
        assert popup.evaluate(grant_marker, grant_key) is True
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
        page.goto(guide_base + 'public'); page.bring_to_front()
        submitted = popup.evaluate("chrome.runtime.sendMessage({action:'submit'})")
        assert submitted['ok']
        assert run_once()
        assert stats() == (3, 2)
        paused_url = guide_base + 'unattended-paused'
        page.goto(paused_url); page.bring_to_front()
        assert popup.evaluate("chrome.runtime.sendMessage({action:'submit'})")['ok']
        stop_extension_worker()
        assert not run_once(), 'terminated permission responder must prevent browser start'
        assert stats() == (3, 2), 'offline consent must not reserve a body slot'
        popup.evaluate("chrome.runtime.sendMessage({action:'resume'})")
        for index, case in enumerate(cases):
            page.goto(guide_base + 'minimal/' + str(index))
            page.bring_to_front()
            before = stats()
            result = popup.evaluate("chrome.runtime.sendMessage({action:'clip'})")
            jobs = popup.evaluate("chrome.storage.local.get('jobs').then(o=>o.jobs)")
            job = next(j for j in jobs.values() if j['url'] == page.url)
            assert result['ok'], case['name']
            assert 'reservation' in job, ('extraction must follow reservation/begin', case['name'])
            assert 'data' not in job, ('no retained raw or rejected body', case['name'])
            accepted = 'reason' not in case
            assert job['state'] == ('acked' if accepted else 'uncertain'), case['name']
            assert stats() == (before[0] + 1, before[1] + int(accepted)), case['name']
            store = Store(state, readonly=True)
            try:
                saved = store.db.execute('SELECT data FROM observations WHERE reservation_id=?',
                                         (job['reservation']['id'],)).fetchone()
                reservation = store.db.execute('SELECT state FROM reservations WHERE id=?',
                                               (job['reservation']['id'],)).fetchone()
                assert reservation[0] == ('drained' if accepted else 'running'), case['name']
                if accepted:
                    data = json.loads(saved[0])
                    assert data['title'] == case['title'], case['name']
                    evidence = data['evidence_locator'][0]
                    assert evidence['locator'] == case['locator'], case['name']
                    assert ' '.join(evidence['quote'].split()) == case['excerpt'], case['name']
                    assert [f['value'] for f in data['extracted_facts']] == (['30000'] if case['name'] == 'valid-main' else []), case['name']
                else:
                    assert saved is None, case['name']
                    record = json.loads(store.db.execute('SELECT data FROM records WHERE id=?',
                                                        (job['reservation']['record_id'],)).fetchone()[0])
                    assert record['extracted_facts'] == record['evidence_locator'] == [], case['name']
                assert store.db.execute('SELECT count(*) FROM archives').fetchone()[0] == 0
            finally:
                store.close()
            # Extraction failures are ambiguous jobs until explicit resumption revokes
            # them. Drain before trying the next document on this same origin.
            if not accepted:
                popup.evaluate("chrome.runtime.sendMessage({action:'resume'})")
        print('shared real clipper fixtures after reservation/begin: acceptance, sanitized evidence and rejection OK')
        policy = next(p for p in cfg['policies'] if p['origin'] == source_origin)
        cfg['policies'].remove(policy)
        assert popup.evaluate('pattern => chrome.permissions.remove({origins:[pattern]})', source_origin + '/*')
        deadline = time.monotonic() + 10
        while popup.evaluate(grant_marker, grant_key) and time.monotonic() < deadline:
            popup.wait_for_timeout(100)
        assert popup.evaluate(grant_marker, grant_key) is None, 'worker did not acknowledge withdrawal'
        assert not popup.evaluate('pattern => chrome.permissions.contains({origins:[pattern]})', source_origin + '/*')
        denied = popup.evaluate("chrome.runtime.sendMessage({action:'submit'})")
        assert not denied['ok']
        store = Store(state, readonly=True)
        try:
            assert store.db.execute('SELECT enabled FROM grants WHERE origin=? AND extension_origin=?',
                                    (source_origin, extension_origin)).fetchone()[0] == 0
            paused = json.loads(store.db.execute('SELECT data FROM records WHERE url=?', (paused_url,)).fetchone()[0])
            assert paused['recheck_requested'] and paused['collected_at'] is None
        finally: store.close()
        before = stats()
        cfg['policies'].append(policy)
        assert not run_once(), 'restored source approval must not restore withdrawn Chrome consent'
        assert stats() == before, 'withdrawn consent must not reserve a body slot or collect the queued URL'
        context.close()
        control['stop'] = True; thread.join(timeout=5)
    print('real Chrome extension: offline, ACK loss, service-worker termination, begin loss, permission withdrawal OK')


if __name__ == '__main__': run(sys.argv[1], sys.argv[2])
