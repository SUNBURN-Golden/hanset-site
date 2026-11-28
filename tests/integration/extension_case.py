"""Real headed Chromium extension lifecycle tests, invoked in the isolated test LAN.

Xvfb/xdotool drive the browser's actual optional-host permission prompt.
"""
from datetime import datetime
import http.server
import io
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
from crawler.rate_limiter import KST
from crawler.store import Store


def run(config_path, temp):
    cfg = load(config_path)
    source_origin = 'https://pubmed.ncbi.nlm.nih.gov'
    guide_base = source_origin + '/guides/'
    grant_key = 'grant:' + source_origin
    cases = [case for case in json.loads((REPO / 'tests/fixtures/minimal_regions.json').read_text())
             if not case.get('http_only')]
    state = Path(temp) / 'extension.sqlite3'
    control = {'drop_result': None, 'drop_begin': False, 'stop': False, 'clock_offset': 0}
    results = []
    ready = threading.Event()
    # Keep deliberate lease-expiry jumps away from the collector's midnight cutoff.
    clock_start = datetime(2026, 1, 1, 12, tzinfo=KST).timestamp()
    monotonic_start = time.monotonic()

    def clock():
        return clock_start + (time.monotonic() - monotonic_start) + control['clock_offset']

    def server_thread():
        store = Store(state)
        worker = Worker(store, cfg, clock=clock)
        base = handler(worker)
        class FaultIntake(base):
            def disconnect(self):
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()

            def do_POST(self):
                if self.path != '/result':
                    return super().do_POST()
                original = self.rfile
                raw = original.read(int(self.headers['Content-Length']))
                self.result_entry = {'request': json.loads(raw), 'response': None}
                results.append(self.result_entry)
                if control['drop_result'] == 'before':
                    control['drop_result'] = None
                    self.disconnect()
                    return
                try:
                    self.rfile = io.BytesIO(raw)
                    super().do_POST()
                finally:
                    self.rfile = original

            def reply(self, status, value):
                if hasattr(self, 'result_entry'):
                    self.result_entry['response'] = (status, value)
                    if control['drop_result'] == 'after' and status == 200:
                        control['drop_result'] = None
                        self.disconnect()
                        return
                if self.path == '/begin' and control['drop_begin'] and status == 200:
                    control['drop_begin'] = False
                    self.disconnect()
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

    def reservations_for(url):
        store = Store(state, readonly=True)
        try:
            return [dict(row) for row in store.db.execute(
                'SELECT * FROM reservations WHERE url=? ORDER BY generation', (url,))]
        finally: store.close()

    def observed_reservations(record_id):
        store = Store(state, readonly=True)
        try:
            return [row[0] for row in store.db.execute(
                'SELECT reservation_id FROM observations WHERE record_id=?', (record_id,))]
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

        def send(action):
            return popup.evaluate('action => chrome.runtime.sendMessage({action})', action)

        def expect_state(action, state):
            assert send(action) == {'ok': True, 'state': state}

        def queue():
            return popup.evaluate("chrome.storage.local.get('jobs').then(o => o.jobs || {})")

        def saved_job(url):
            return next(job for job in queue().values() if job['url'] == url)

        def watch_dom(page):
            page.bring_to_front()
            return popup.evaluate('''async () => {
                const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
                const [injected] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: () => {
                    const querySelector = Document.prototype.querySelector;
                    globalThis.fixtureDOMReads = 0;
                    Document.prototype.querySelector = function (selector) {
                        if (selector === 'main, article') globalThis.fixtureDOMReads++;
                        return querySelector.call(this, selector);
                    };
                }});
                return {tabId: tab.id, documentIds: [injected.documentId]};
            }''')

        def dom_reads(target):
            return popup.evaluate('''async target => {
                const [read] = await chrome.scripting.executeScript({target, func: () => globalThis.fixtureDOMReads});
                return read.result;
            }''', target)

        def advance_past(reservation):
            control['clock_offset'] += max(0, reservation['expires'] + 1 - clock())

        page = context.new_page()
        page.goto(guide_base + 'manual-one')
        watched = watch_dom(page)
        # Offline: a real scripting API reads URL metadata only, never the region.
        expect_state('clip', 'intent')
        job = saved_job(page.url)
        assert job['state'] == 'intent' and 'data' not in job
        assert dom_reads(watched) == 0
        thread = threading.Thread(target=server_thread, daemon=True); thread.start()
        assert ready.wait(5)
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

        def drop_clip_result(when):
            control['drop_result'] = when
            start = len(results)
            expect_state('clip', 'ready')
            job = saved_job(page.url)
            assert job['state'] == 'ready'
            assert len(results) == start + 1 and control['drop_result'] is None
            assert results[start]['request'] == {'reservation': job['reservation'], 'data': job['data']}
            return job, start

        # Committed results replay the same ACK even after the saved lease expires.
        job, start = drop_clip_result('after')
        reservation = job['reservation']
        ack = {'ack': reservation['id'], 'record_id': reservation['record_id']}
        assert results[start]['response'] == (200, ack)
        assert 'never-store' not in json.dumps(queue())
        assert job['data']['excerpt'] == 'Independent public guide'
        assert stats() == (1, 1)
        assert observed_reservations(reservation['record_id']) == [reservation['id']]
        stop_extension_worker()
        advance_past(reservation)
        expect_state('resume', 'resumed')
        assert results[start + 1:] == [results[start]]
        assert job['clip_id'] not in queue()
        assert dom_reads(watched) == 1 and stats() == (1, 1)
        assert observed_reservations(reservation['record_id']) == [reservation['id']]

        # When the request is lost before acceptance, the expired replay is final.
        page.goto(guide_base + 'expired')
        watched = watch_dom(page)
        before = stats()
        job, start = drop_clip_result('before')
        reservation = job['reservation']
        assert results[start]['response'] is None
        stop_extension_worker()
        advance_past(reservation)
        expect_state('resume', 'resumed')
        assert results[start + 1:] == [{'request': results[start]['request'],
                                        'response': (409, {'reason': 'lease_expired_or_revoked'})}]
        assert job['clip_id'] not in queue()
        expect_state('resume', 'resumed')
        assert len(results) == start + 2
        assert observed_reservations(reservation['record_id']) == []
        assert dom_reads(watched) == 1 and stats() == (before[0] + 1, before[1])

        # A second tab can clip the same URL once the old reservation has drained.
        page.goto(guide_base + 'superseded')
        watched = watch_dom(page)
        before = stats()
        job, start = drop_clip_result('before')
        older = job['reservation']
        assert results[start]['response'] is None
        stop_extension_worker()
        advance_past(older)
        deadline = time.monotonic() + 5
        while reservations_for(page.url)[0]['state'] != 'drained' and time.monotonic() < deadline:
            popup.wait_for_timeout(100)
        assert reservations_for(page.url)[0]['state'] == 'drained'
        second = context.new_page()
        second.goto(page.url)
        watched_second = watch_dom(second)
        expect_state('clip', 'acked')
        previous, newer = reservations_for(second.url)
        assert previous['id'] == older['id']
        assert newer['record_id'] == older['record_id'] and newer['generation'] > older['generation']
        assert newer['clip_id'] not in queue()
        assert results[start + 1]['request']['reservation']['id'] == newer['id']
        assert results[start + 1]['response'] == (200, {'ack': newer['id'], 'record_id': older['record_id']})
        expect_state('resume', 'resumed')
        assert results[start + 2:] == [{'request': results[start]['request'],
                                        'response': (409, {'reason': 'stale_generation'})}]
        assert job['clip_id'] not in queue()
        assert dom_reads(watched) == dom_reads(watched_second) == 1
        assert stats() == (before[0] + 2, before[1] + 1)
        assert observed_reservations(older['record_id']) == [newer['id']]
        second.close()

        # Lost begin leaves the uncertain checkpoint and no extraction after restart.
        page.goto(guide_base + 'manual-two')
        watched = watch_dom(page)
        before = stats()
        start = len(results)
        control['drop_begin'] = True
        expect_state('clip', 'uncertain')
        job = saved_job(page.url)
        assert job['state'] == 'uncertain' and 'data' not in job
        assert dom_reads(watched) == 0
        stop_extension_worker()
        expect_state('resume', 'resumed')
        assert queue() == {job['clip_id']: job}
        assert len(results) == start and dom_reads(watched) == 0
        assert stats() == (before[0] + 1, before[1])
        assert observed_reservations(job['reservation']['record_id']) == []
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
                try: outcomes.append(Worker(local, cfg, clock=clock).once())
                finally: local.close()
            thread = threading.Thread(target=execute, daemon=True)
            thread.start()
            while thread.is_alive():
                popup.wait_for_timeout(100)
            thread.join()
            assert len(outcomes) == 1
            return outcomes[0]
        page.goto(guide_base + 'public'); page.bring_to_front()
        before = stats()
        submitted = send('submit')
        assert submitted['ok']
        assert run_once()
        collected = (before[0] + 1, before[1] + 1)
        assert stats() == collected
        paused_url = guide_base + 'unattended-paused'
        page.goto(paused_url); page.bring_to_front()
        assert send('submit')['ok']
        stop_extension_worker()
        assert not run_once(), 'terminated permission responder must prevent browser start'
        assert stats() == collected, 'offline consent must not reserve a body slot'
        expect_state('resume', 'resumed')
        for index, case in enumerate(cases):
            page.goto(guide_base + 'minimal/' + str(index))
            page.bring_to_front()
            before = stats()
            accepted = 'reason' not in case
            assert send('clip') == {'ok': True, 'state': 'acked' if accepted else 'uncertain'}, case['name']
            reservation, = reservations_for(page.url)
            if accepted:
                assert reservation['clip_id'] not in queue(), case['name']
            else:
                job = queue()[reservation['clip_id']]
                assert job['state'] == 'uncertain' and 'data' not in job, case['name']
            assert all(j['state'] not in ('acked', 'expired') for j in queue().values())
            assert stats() == (before[0] + 1, before[1] + int(accepted)), case['name']
            store = Store(state, readonly=True)
            try:
                saved = store.db.execute('SELECT data FROM observations WHERE reservation_id=?',
                                         (reservation['id'],)).fetchone()
                assert reservation['state'] == ('drained' if accepted else 'running'), case['name']
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
                                                        (reservation['record_id'],)).fetchone()[0])
                    assert record['extracted_facts'] == record['evidence_locator'] == [], case['name']
                assert store.db.execute('SELECT count(*) FROM archives').fetchone()[0] == 0
            finally:
                store.close()
            # Extraction failures are ambiguous jobs until explicit resumption revokes
            # them. Drain before trying the next document on this same origin.
            if not accepted:
                expect_state('resume', 'resumed')
        print('shared real clipper fixtures after reservation/begin: acceptance, sanitized evidence and rejection OK')
        policy = next(p for p in cfg['policies'] if p['origin'] == source_origin)
        cfg['policies'].remove(policy)
        assert popup.evaluate('pattern => chrome.permissions.remove({origins:[pattern]})', source_origin + '/*')
        deadline = time.monotonic() + 10
        while popup.evaluate(grant_marker, grant_key) and time.monotonic() < deadline:
            popup.wait_for_timeout(100)
        assert popup.evaluate(grant_marker, grant_key) is None, 'worker did not acknowledge withdrawal'
        assert not popup.evaluate('pattern => chrome.permissions.contains({origins:[pattern]})', source_origin + '/*')
        denied = send('submit')
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
    print('real Chrome extension: offline, ACK replay, expiry, supersession, begin loss, permission withdrawal OK')


if __name__ == '__main__': run(sys.argv[1], sys.argv[2])
