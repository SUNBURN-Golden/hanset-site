"""Offline regressions for host policy, grants and escaped discovery links."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
import fcntl
import json
import sqlite3
from threading import Event
from types import SimpleNamespace

import pytest
import requests

from hanset_active.collect import service
from hanset_active.collect.store import digest
from hanset_active.collect.targets import LINK_IDENTITY
from hanset_active.crawl_policy import COLLECTION
from hanset_active.db import connect
from hanset_active.fetcher import USER_AGENT, Fetcher
from hanset_active.host_policy import HostPolicy, resource_id
from hanset_active.locks import hostname
from hanset_active.network_store import AUDIT_FILE, LEASE_FILE
from hanset_active.search.tavily import SearchLead, SearchOutcome
from hanset_active.util.timeutil import FakeClock
from tests.active_bot.test_collect_subpages import capture_extraction, html
from tests.active_bot.test_collect_web import _settings, _targets


ROOT = 'https://spa.example.test'
NEWS = 'https://news.example.test'


def reply(status=200, body=b'', **headers):
    return SimpleNamespace(status_code=status, headers={'Content-Type': 'text/html', **headers},
                           iter_content=lambda _: iter([body]), close=lambda: None)


def assert_host_locked(host_dir, url):
    lock = host_dir / 'host-locks' / (hostname(url) + '.lock')
    with lock.open('r') as peer:
        with pytest.raises(BlockingIOError):
            fcntl.flock(peer, fcntl.LOCK_EX | fcntl.LOCK_NB)


def fake_requests(monkeypatch, host_dir, route):
    calls = []

    def get(self, url, *, headers, allow_redirects, **kwargs):
        assert headers['User-Agent'] == USER_AGENT
        assert not allow_redirects
        assert_host_locked(host_dir, url)
        calls.append(url)
        return route(url)

    monkeypatch.setattr(requests.Session, 'get', get)
    return calls


def test_non_news_is_serial_without_spacing_and_news_delay_is_durable(bot, monkeypatch):
    monkeypatch.setattr('hanset_active.crawl_policy._NEWS_DOMAINS', {'news.example.test'})
    timeline = []

    def route(url):
        timeline.append((url, bot.clock.monotonic()))
        if url.endswith('/robots.txt'):
            return reply(body=b'User-agent: *\nDisallow: /private\nCrawl-delay: 20\n')
        return reply(body=html())

    calls = fake_requests(monkeypatch, bot.settings.data_dir, route)
    bot.policy = COLLECTION
    bot.public_scope = True
    for url in (ROOT, ROOT + ':8443/hours', ROOT.replace('https:', 'http:') + '/other'):
        assert bot.fetch(url).ok
    assert all(at == 0 for _, at in timeline)
    assert bot.fetch(NEWS + '/hours').ok
    with closing(Fetcher(bot.settings, bot.db, bot.clock, public_scope=True, policy=COLLECTION)) as second:
        assert second.fetch(NEWS + '/other').ok
        assert second.fetch(NEWS + '/private').outcome == 'robots_disallowed'
    assert timeline[3:] == [(NEWS + '/robots.txt', 0), (NEWS + '/hours', 20), (NEWS + '/other', 40)]
    assert NEWS + '/private' not in calls


def test_host_lock_covers_stream_and_allows_a_sibling_host(bot, monkeypatch):
    streaming, release = Event(), Event()

    def route(url):
        response = reply(body=html())
        if url == ROOT:
            def chunks(_size):
                streaming.set()
                assert release.wait(5)
                yield html()
            response.iter_content = chunks
        return response

    fake_requests(monkeypatch, bot.settings.data_dir, route)
    bot.policy = COLLECTION
    bot.public_scope = True
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(bot.fetch, ROOT)
        try:
            assert streaming.wait(5)
            assert_host_locked(bot.settings.data_dir, ROOT + ':8443/hours')
            assert workers.submit(bot.fetch, 'https://other.example.test/hours').result(timeout=5).ok
        finally:
            release.set()
        assert first.result(timeout=5).ok
    assert bot.clock.monotonic() == 0


def test_robots_redirect_and_failed_reservation_retry(bot, monkeypatch):
    monkeypatch.setattr('hanset_active.crawl_policy._NEWS_DOMAINS', {'example.test'})
    mirror = 'https://cdn.example.test/robots.txt'
    responses = iter((reply(301, Location=mirror), reply(500), reply(301, Location=mirror),
                      reply(body=b'User-agent: *\nDisallow: /private\n'), reply(body=html())))
    calls = fake_requests(monkeypatch, bot.settings.data_dir, lambda _: next(responses))
    bot.policy = COLLECTION
    bot.public_scope = True
    assert bot.fetch(NEWS + '/hours').reason == 'robots_unavailable'
    assert bot.fetch(NEWS + '/hours').ok
    assert calls == [NEWS + '/robots.txt', mirror, NEWS + '/robots.txt', mirror, NEWS + '/hours']


@pytest.mark.parametrize('status', [404, 410])
def test_terminal_robots_absence_caches_even_with_an_html_error_page(bot, monkeypatch, status):
    monkeypatch.setattr('hanset_active.crawl_policy._NEWS_DOMAINS', {'news.example.test'})
    calls = fake_requests(monkeypatch, bot.settings.data_dir, lambda url:
                          reply(status, b'<h1>Not Found</h1>') if url.endswith('/robots.txt') else reply(body=html()))
    bot.policy = COLLECTION
    bot.public_scope = True
    assert bot.fetch(NEWS + '/hours').ok
    assert bot.fetch(NEWS + '/price').ok
    assert calls == [NEWS + '/robots.txt', NEWS + '/hours', NEWS + '/price']


@pytest.mark.parametrize('status', [429, 503])
def test_retry_after_survives_an_interrupted_audit_callback(bot, monkeypatch, status):
    blocked = ROOT + '/hours'
    calls = fake_requests(monkeypatch, bot.settings.data_dir,
                          lambda _: reply(status, **{'Retry-After': '900'}))
    bot.policy = COLLECTION
    bot.public_scope = True

    def interrupt(*args):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(bot, '_log_request', interrupt)
        with pytest.raises(KeyboardInterrupt):
            bot.fetch(blocked)
    assert bot.fetch(ROOT + '/later').reason == 'host_backoff'
    with closing(Fetcher(bot.settings, bot.db, bot.clock, public_scope=True, policy=COLLECTION)) as second:
        result = second.fetch(ROOT + ':8443/other')
        assert result.reason == 'host_backoff'
        assert datetime.fromisoformat(result.retry_after) == bot.clock.now() + timedelta(seconds=900)
    assert calls == [blocked]
    assert bot.host_policy.db.execute('SELECT count(*) FROM resource_blocks').fetchone()[0] == 1


def test_third_strike_cannot_shorten_a_resource_hold(tmp_path):
    clock = FakeClock()
    with closing(HostPolicy(tmp_path, clock, 5)) as policy:
        policy.block(ROOT, '')
        clock.sleep(1)
        policy.block(ROOT, '')
        hold = policy.stopped(ROOT)[1]
        clock.sleep(1)
        policy.block(ROOT, '')
        policy.refuse(ROOT)
        assert policy.db.execute('SELECT hold_until FROM resource_blocks WHERE url_hash=?',
                                 (resource_id(ROOT),)).fetchone()[0] >= hold


def test_missing_active_stays_missing_and_escaped_child_has_its_own_identity(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    path = settings.data_dir / 'web_targets.json'
    _targets(path, ROOT, name='Seed Spa A')
    child = ROOT + '/guide/price%23weekend/'
    pages = {ROOT: html('<a href="/guide/price%23weekend/">Price</a>'), child: html()}
    calls = fake_requests(monkeypatch, settings.active_dir, lambda url: reply(body=pages[url]))
    capture_extraction(monkeypatch)
    clock = FakeClock()
    assert service.run_web(settings, clock=clock, testing=True)['success']
    assert service.run_web(settings, clock=clock, testing=True)['state'] == 'already_complete'
    assert calls == [ROOT, child]
    assert not (settings.active_dir / 'active.sqlite3').exists()
    with closing(sqlite3.connect(settings.active_dir / AUDIT_FILE)) as audit:
        assert audit.execute('SELECT url FROM request_log ORDER BY request_id').fetchall() == [(ROOT,), (child,)]
    with closing(sqlite3.connect(settings.active_dir / LEASE_FILE)) as leases:
        assert not leases.execute('SELECT * FROM run_lease').fetchall()
    with closing(sqlite3.connect(settings.data_dir / 'collector.sqlite3')) as db:
        key = child.rstrip('/')
        assert db.execute('SELECT requested_url,identity_mode FROM receipts WHERE source_url=?', (child,)).fetchone() == (key, LINK_IDENTITY)
        assert db.execute('SELECT identity_mode FROM web_target_observations WHERE url_hash=?',
                          (digest(key),)).fetchone() == (LINK_IDENTITY,)


def test_search_grants_survive_url_dedup_and_retire_on_no_targets(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    connect(settings.active_dir / 'active.sqlite3').close()
    path = settings.data_dir / 'web_targets.json'
    lead = ROOT + '/hours'
    searches = []

    def write(*fields):
        path.write_text(json.dumps({'schema_version': 1, 'targets': [
            {'venue_id': 'v', 'venue_key': 'seed-spa-a', 'name': 'Seed Spa A', 'official_urls': [],
             'official_domains': ['example.test', 'other.test'] if field == 'hours' else ['example.test'],
             'tavily': {'venue_id': 'v', 'field': field}}
            for field in fields]}))

    def search(_db, _venue, field, **kwargs):
        searches.append(field)
        if len(searches) > 2:
            return SearchOutcome('error', 'tavily_transport')
        return SearchOutcome('ok', 'ok', leads=(SearchLead(lead, '', ''),))

    calls = fake_requests(monkeypatch, settings.active_dir, lambda _: reply(body=html()))
    capture_extraction(monkeypatch)
    clock = FakeClock()

    def run():
        return service.run_web(settings, search=search, clock=clock, testing=True)

    def bindings():
        with closing(sqlite3.connect(settings.data_dir / 'collector.sqlite3')) as db:
            return (db.execute('SELECT binding_current FROM receipts').fetchone()[0],
                    db.execute('SELECT binding_current FROM web_receipt_scopes').fetchone()[0],
                    db.execute('SELECT count(*) FROM web_search_grants').fetchone()[0])

    write('hours', 'price')
    first = run()
    assert first['success'], first
    assert bindings() == (1, 1, 2)
    assert calls == [lead] and searches == ['hours', 'price']
    write('price')
    assert run()['state'] == 'already_complete' and bindings() == (1, 1, 1)
    write()
    assert run()['state'] == 'no_targets' and bindings() == (0, 0, 0)
    assert calls == [lead]


def test_seed_receipt_survives_missing_snapshot_and_retires_when_restored(tmp_path, monkeypatch):
    from hanset_active.venues import seed

    settings = _settings(tmp_path)
    active = settings.active_dir / 'active.sqlite3'
    url = 'https://runport.example.test/hours'
    clock = FakeClock()
    with closing(connect(active)) as db:
        seed(db, [{'name': 'Seed Spa A', 'website': url}], clock=clock)
    calls = fake_requests(monkeypatch, settings.active_dir, lambda _: reply(body=html()))
    capture_extraction(monkeypatch)
    assert service.run_web(settings, clock=clock, testing=True)['success']

    def binding():
        with closing(sqlite3.connect(settings.data_dir / 'collector.sqlite3')) as db:
            return db.execute('SELECT authorization_json,binding_current FROM receipts WHERE source_url=?', (url,)).fetchone()

    offline = active.with_name('fixture-offline.sqlite3')
    active.rename(offline)
    assert service.run_web(settings, clock=clock, testing=True)['state'] == 'no_targets'
    assert not active.exists() and binding() == (None, 1)
    offline.rename(active)
    with closing(connect(active, create=False)) as db, db:
        db.execute('DELETE FROM run_checkpoints WHERE checkpoint_id=?', ('seed_freeze',))
    assert service.run_web(settings, clock=clock, testing=True)['state'] == 'no_targets'
    assert binding() == (None, 0) and calls == [url]
