"""Offline policy tests across the Active and Linux collector entry points."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from crawler import network_gate
from crawler.config import Rejected, load, policy_for
from crawler.local_worker import Worker, uid
from crawler.store import Store
from hanset_active import web_proxy
from hanset_active.collect import runtime, service
from hanset_active.collect.config import official_site
from hanset_active.collect.state import State
from hanset_active.collect.store import (
    connect, digest, recent_web_target_block, remember_web_target_block,
    remember_web_target_observation, reopen_non_news_robots_holds,
)
from hanset_active.collect.web import select_subpages
from hanset_active.crawl_policy import COLLECTION, DISCOVERY, LEGACY, KoreanNewsRobots, is_korean_news
from hanset_active.db import connect as active_connect
from hanset_active.discover.sitemaps import robots_sitemaps
from hanset_active.fetcher import USER_AGENT, Fetcher
from hanset_active.util.timeutil import FakeClock
from tests.active_bot.test_collect_retry4 import fake_requests, reply
from tests.active_bot.test_collect_subpages import capture_extraction, fake_pages, html, setup_run
from tests.test_collector_pipeline import MemoryResponse


@pytest.mark.parametrize('host,news', [
    ('n.news.naver.com', True), ('WWW.YNA.CO.KR.', True), ('news.daum.net', True),
    ('blog.naver.com', False), ('map.naver.com', False), ('spa.example.test', False),
    ('naver.com', False), ('daum.net', False), ('news.naver.com.example.test', False),
])
def test_is_korean_news(host, news):
    assert is_korean_news(f'https://{host}/visit') is news


@pytest.mark.parametrize('policy', [LEGACY, COLLECTION, DISCOVERY])
def test_legacy_robots_cache_does_not_gate_non_news(bot, monkeypatch, policy):
    url = 'https://spa.example.test'
    stamp = bot.clock.now().isoformat()
    with bot.db:
        bot.db.execute('INSERT INTO robots_cache VALUES (?,?,?,?,?,?,?)',
                       (url, 'User-agent: *\nDisallow: /', 200, stamp,
                        '2099-01-01T00:00:00+09:00', 0, 'robots unavailable'))
    calls = fake_requests(monkeypatch, bot.settings.data_dir, lambda _: reply(body=html()))
    bot.policy = policy
    for suffix in ('/one', '/two'):
        assert bot.fetch(url + suffix).ok
    assert calls == [url + '/one', url + '/two']
    assert bot.clock.monotonic() == 0
    assert not bot.host_policy.db.execute('SELECT 1 FROM host_timing').fetchone()


def test_non_news_same_host_never_overlaps_and_sibling_progresses(bot, monkeypatch):
    started, release, waiting = Event(), Event(), Event()
    active = peak = 0
    root = 'https://spa.example.test'
    second_db = active_connect(bot.settings.data_dir / 'test.sqlite3')
    second = Fetcher(bot.settings, second_db, bot.clock)

    def route(url):
        response = reply(body=html())
        if url.startswith(root):
            def stream(_):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                try:
                    if url == root:
                        started.set()
                        assert release.wait(5)
                    yield html()
                finally:
                    active -= 1
            response.iter_content = stream
        return response

    def competing_fetch():
        waiting.set()
        return second.fetch(root + '/second')

    calls = fake_requests(monkeypatch, bot.settings.data_dir, route)
    with closing(second_db), closing(second), ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(bot.fetch, root)
        try:
            assert started.wait(5)
            same = pool.submit(competing_fetch)
            assert waiting.wait(5)
            assert pool.submit(bot.fetch, 'https://other.example.test').result(timeout=5).ok
            assert calls == [root, 'https://other.example.test']
        finally:
            release.set()
        assert first.result(timeout=5).ok and same.result(timeout=5).ok
    assert peak == 1 and calls[-1] == root + '/second'
    assert bot.clock.monotonic() == 0


@pytest.mark.parametrize('ua,other', [(USER_AGENT, 'HANSET-Collector'),
                                      (network_gate.USER_AGENT, 'HansetActiveBot')])
def test_robots_group_boundary_with_zero_delay(ua, other):
    product = ua.split('/')[0]
    parser = KoreanNewsRobots(
        f'User-agent: Unrelated\nUser-agent: {product}\nCrawl-delay: 0\n'
        f'User-agent: {other}\nDisallow: /\nCrawl-delay: 20\nUser-agent: *\nDisallow: /',
        user_agent=ua)
    assert parser.delay == 0 and parser.can_fetch('https://news.example.test/hours')


def test_fetch_paths_use_the_fixed_hanset_user_agent():
    from crawler import network_gate
    from hanset_active.fetcher import USER_AGENT as ACTIVE_UA

    for user_agent in (ACTIVE_UA, network_gate.USER_AGENT):
        assert 'hanset' in user_agent.casefold() and 'hanset@soulbound.co.kr' in user_agent
    # Only the real desktop browser (extract/browser.py) may imitate a browser.
    root = Path(__file__).resolve().parents[2]
    sources = [*sorted((root / 'crawler').glob('*.py')), root / 'hanset_active' / 'fetcher.py']
    for path in sources:
        source = path.read_text(encoding='utf-8')
        assert not re.search(r'Mozilla|AppleWebKit|Chrome/|Safari/', source), path


def test_redirect_hops_classified_independently(bot, monkeypatch):
    spa, news = 'https://spa.example.test', 'https://news.example.test'
    monkeypatch.setattr('hanset_active.crawl_policy._NEWS_DOMAINS', {'news.example.test'})
    calls = fake_requests(monkeypatch, bot.settings.data_dir, {
        spa + '/entry': reply(302, Location=news + '/private/'),
        news + '/robots.txt': reply(body=b'User-agent: *\nDisallow: /private/'),
        news + '/entry': reply(302, Location=spa + '/safe'),
        spa + '/safe': reply(body=html()),
    }.__getitem__)
    assert bot.fetch(news + '/entry').ok
    result = bot.fetch(spa + '/entry')
    assert (result.outcome, result.url) == ('robots_disallowed', news + '/private/')
    assert calls == [news + '/robots.txt', news + '/entry', spa + '/safe', spa + '/entry']
    assert bot.db.execute("SELECT url FROM failures WHERE reason='robots_disallowed'").fetchone()[0] == result.url


def test_naver_transport_allowed_without_website_authority(bot, monkeypatch):
    blog, place = 'https://blog.naver.com/seed-spa-a', 'https://map.naver.com/seed-spa-a'
    calls = fake_requests(monkeypatch, bot.settings.data_dir, lambda _: reply(body=html()))
    assert bot.fetch(blog).ok and bot.fetch(place).ok
    assert bot.fetch('https://instagram.com/seed-spa-a').outcome == 'denied'
    assert calls == [blog, place]
    for url in (blog, place, 'https://naver.com', 'https://www.yna.co.kr', 'https://joongang.co.kr'):
        assert not official_site(url)
    content = b'<a href="/hours">Opening hours</a>'
    assert not select_subpages(content, blog, {'naver.com'})
    assert not select_subpages(content, 'https://www.yna.co.kr', {'yna.co.kr'})


def test_useful_naver_link_becomes_a_third_party_trial(bot):
    from hanset_active.discover import Discovery
    from hanset_active.discover.links import discover
    from hanset_active.extract.html import extract

    origin = 'https://spa.example.test'
    blog = 'https://blog.naver.com/seed-spa-a'
    document = extract(html(f'<a href="{blog}">Opening hours</a>'), origin)
    assert document['links'][0]['kind'] == 'external'
    task, = discover(Discovery(bot), document, parent_url=origin)
    assert tuple(bot.db.execute('SELECT kind,url FROM tasks WHERE task_id=?', (task,)).fetchone()) == ('trial', blog)
    assert not official_site(blog)


def test_non_news_robots_can_supply_sitemaps_without_gating(bot, monkeypatch):
    from hanset_active.discover import Discovery

    url = 'https://spa.example.test'
    routes = {
        url + '/robots.txt': reply(body=f'User-agent: *\nDisallow: /\nSitemap: {url}/extra.xml'.encode(),
                                   **{'Content-Type': 'text/plain'}),
        url + '/sitemap.xml': reply(404),
        url + '/extra.xml': reply(body=b'<urlset></urlset>', **{'Content-Type': 'application/xml'}),
        url + '/hours': reply(body=html()),
    }
    calls = fake_requests(monkeypatch, bot.settings.data_dir, routes.__getitem__)
    robots_sitemaps(Discovery(bot), url)
    assert bot.fetch(url + '/hours').ok
    assert calls == list(routes)
    assert not bot.db.execute("SELECT 1 FROM failures WHERE reason='robots_disallowed'").fetchone()


def test_streaming_proxy_outage_discards_body_and_uses_direct_next(bot, monkeypatch, caplog):
    proxy = 'socks5h://127.0.0.1:1080'
    egress = web_proxy.Egress(mode='proxy', proxy=proxy)
    first, second = 'https://spa.example.test/first', 'https://spa.example.test/second'
    calls = []
    close = Mock()

    def partial(_):
        yield b'partial response fixture'
        raise requests.exceptions.ChunkedEncodingError('interrupted')

    def get(_, url, *, proxies, **kwargs):
        calls.append((url, proxies['https']))
        response = reply(body=html())
        if url == first:
            response.iter_content = partial
            response.close = close
        return response

    monkeypatch.setattr(requests.Session, 'get', get)
    monkeypatch.setattr(web_proxy, 'reachable', lambda _: False)
    with closing(Fetcher(bot.settings, bot.db, bot.clock, egress=egress)) as fetcher:
        result = fetcher.fetch(first)
        assert (result.outcome, result.reason, result.content) == ('error', 'proxy_unavailable', b'')
        assert fetcher.fetch(second).ok
    close.assert_called_once()
    assert calls == [(first, proxy), (second, None)]
    assert egress.public() == {'event': 'proxy_fallback_direct', 'mode': 'direct',
                               'reason': 'proxy_unavailable', 'phase': 'mid_run'}
    assert not egress.fallback('proxy_unavailable', 'mid_run')
    assert sum('proxy_fallback_direct' in record.message for record in caplog.records) == 1
    assert 'partial response fixture' not in caplog.text
    assert tuple(bot.db.execute('SELECT bytes,transfer_complete,outcome FROM request_log ORDER BY request_id').fetchone()) == (
        0, 0, 'proxy_unavailable')


def test_proxy_does_not_switch_after_partial_body_establishes_a_block(bot, monkeypatch):
    probe = Mock(return_value=False)
    monkeypatch.setattr(web_proxy, 'reachable', probe)

    def partial(_):
        yield b'<main>Subscribers only. Subscription is required to view this page.</main>'
        raise requests.exceptions.ChunkedEncodingError('interrupted')

    response = reply()
    response.iter_content = partial
    path = 'https://spa.example.test/hours'
    calls = fake_requests(monkeypatch, bot.settings.data_dir, lambda _: response)
    with closing(Fetcher(bot.settings, bot.db, bot.clock, proxy='socks5h://127.0.0.1:1080')) as fetcher:
        result = fetcher.fetch(path)
        assert (result.outcome, result.content) == ('blocked', b'')
        assert fetcher.egress.public() is None
        assert fetcher.fetch(path).reason == 'resource_hold'
    assert calls == [path]
    probe.assert_not_called()


@pytest.mark.parametrize('entry', ['seed', 'scheduled'])
def test_proxy_preflight_fallback_in_seed_and_scheduled_runs(bot, monkeypatch, capsys, caplog, entry):
    from hanset_active.collect import scheduled
    from hanset_active.discover import seeds

    monkeypatch.setenv(web_proxy.ENV, 'socks5h://127.0.0.1:1080')
    probe = Mock(return_value='proxy_exit_mismatch')
    monkeypatch.setattr(web_proxy, 'preflight', probe)
    expected = {'event': 'proxy_fallback_direct', 'mode': 'direct',
                'reason': 'proxy_exit_mismatch', 'phase': 'preflight'}

    if entry == 'seed':
        seen = []

        def directories(web):
            seen.append(web.fetcher.egress.public())
            assert web.fetcher.egress.mode == 'direct'
            return Counter(), 'ok'

        monkeypatch.setattr(seeds.WebSeeds, 'directories', directories)
        result = seeds.discover(bot.settings, bot.db, sources=['directories'], clock=bot.clock)
        assert result['state'] == 'ok' and result['egress'] == expected
        assert seen == [expected]
    else:
        def run(fetcher, **_):
            assert fetcher.egress.mode == 'direct'
            return {'checkpoint_complete': True}
        monkeypatch.setattr(scheduled, 'run', run)
        operator = SimpleNamespace(settings=SimpleNamespace(active_dir=bot.settings.data_dir),
                                   clock=bot.clock, testing=True)
        assert scheduled.Scheduled.collect(operator, 'active', None, SimpleNamespace(db=bot.db))
        assert json.loads(capsys.readouterr().out)['web_egress'] == expected
    assert probe.call_count == 3
    assert sum('proxy_fallback_direct' in record.message for record in caplog.records) == 1


def test_web_tavily_quota_stops_further_dispatch(bot, monkeypatch):
    from hanset_active import search
    from hanset_active.discover import seeds
    from hanset_active.search.tavily import SearchOutcome

    run = seeds.TavilyRun()
    dispatched = []
    monkeypatch.setattr(seeds, 'TavilyRun', lambda: run)

    def discover(_db, venue_id, _field, *, dispatch_guard, **kwargs):
        if dispatch_guard():
            dispatched.append(venue_id)
            return SearchOutcome('stopped', 'tavily_provider_quota')
        return SearchOutcome('skipped', 'tavily_run_limit')

    monkeypatch.setattr(search, 'discover_known_venue', discover)
    get_leads = service._default_search(bot.settings.data_dir)
    assert get_leads(bot.db, 'seed-a', 'hours').reason == 'tavily_provider_quota'
    assert run.reason == 'tavily_provider_quota' and not run.search_allowed()
    assert get_leads(bot.db, 'box-b', 'hours').reason == 'tavily_run_limit'
    assert dispatched == ['seed-a']


def test_web_reopen_releases_only_proven_non_news_and_dispatches(tmp_path, monkeypatch):
    requested = 'https://spa.example.test/hours'
    redirected = 'https://spa.example.test/news'
    news = 'https://news.naver.com/private'
    login = 'https://spa.example.test/login'
    unknown = 'https://spa.example.test/unknown'
    settings, targets = setup_run(tmp_path, monkeypatch, [requested, redirected, login, unknown])
    clock = FakeClock()
    state = State(settings.data_dir, clock)
    state.data['completed'][requested] = 'robots_disallowed'
    state.save()
    with closing(connect(settings.data_dir)) as history, closing(runtime.connect(settings.data_dir)) as ledger:
        for url, final, disposition in [
            (requested, requested, 'robots_disallowed'), (redirected, news, 'robots_disallowed'),
            (news, news, 'robots_disallowed'), (login, login, 'login_wall'),
        ]:
            remember_web_target_block(history, url, disposition, status=200, final_url=final, clock=clock)
            runtime.complete_web(ledger, state.data['day'], url, disposition)
        with history:
            history.execute("""INSERT INTO web_target_blocks(url_hash,disposition,blocked_at)
                               VALUES (?,'robots_disallowed',?)""", (digest(unknown), clock.now().isoformat()))
        remember_web_target_observation(history, requested, 'robots_disallowed', observed_at=clock.now().isoformat())
        runtime.complete_web(ledger, '2025-12-31', requested, 'robots_disallowed')
        first = reopen_non_news_robots_holds(history, ledger, state, clock, False)
        assert first == {'robots_hold_reopened': 1, 'robots_hold_news_kept': 2, 'robots_hold_unresolved': 1}
        assert requested not in state.data['completed']
        assert not recent_web_target_block(history, requested, clock=clock)
        assert all(recent_web_target_block(history, url, clock=clock) for url in (redirected, news, login, unknown))
        assert not ledger.execute('SELECT 1 FROM web_completions WHERE requested_url=?', (requested,)).fetchone()
        assert history.execute('SELECT count(*) FROM web_target_blocks').fetchone()[0] == 5
        assert history.execute('SELECT disposition FROM web_target_observations').fetchone()[0] == 'robots_disallowed'
        assert reopen_non_news_robots_holds(history, ledger, state, clock, False)['robots_hold_reopened'] == 0
        with history:
            history.execute('DELETE FROM web_block_releases')
        runtime.complete_web(ledger, state.data['day'], requested, 'robots_disallowed')
        state.complete(requested, 'robots_disallowed')

    calls = fake_pages(monkeypatch, {requested: html()})
    capture_extraction(monkeypatch)
    result = service.run_web(settings, targets_path=targets, clock=clock, testing=True)
    assert result['success'] and result['rejections']['robots_hold_reopened'] == 1
    assert calls == [requested]


@pytest.mark.parametrize('delay', [0, 20])
def test_linux_news_live_robots_and_spacing(tmp_path, monkeypatch, delay):
    monkeypatch.setattr('hanset_active.crawl_policy._NEWS_DOMAINS', {'news.example.test'})
    host = 'news.example.test'
    root = 'https://' + host
    now = datetime(2026, 11, 25, tzinfo=timezone.utc).timestamp()
    times = [now]

    def sleep(seconds):
        times[0] += seconds

    monkeypatch.setattr(network_gate.time, 'sleep', sleep)
    cfg = load()
    cfg['policies'] = [{'origin': root, 'public': True, 'paths': ['/public'],
                        'robots_reviewed_at': '2026-11-25'}]
    with closing(Store(tmp_path / 'linux.sqlite3')) as store:
        worker = Worker(store, cfg, lambda: times[0])
        res = worker.begin(worker.reserve(uid(), root + '/public', uid(), 'auto_discovery'))
        resolve = Mock(return_value=['93.184.216.34'])
        pinned = Mock()
        pinned.return_value.getresponse.side_effect = [
            MemoryResponse(body=f'User-agent: HANSET-Collector\nDisallow: /public/private\nCrawl-delay: {delay}'.encode(),
                           content_type='text/plain'),
            MemoryResponse(), MemoryResponse(),
        ]
        monkeypatch.setattr(network_gate, 'public_addresses', resolve)
        monkeypatch.setattr(network_gate, 'PinnedTLS', pinned)
        gate = network_gate.NetworkGate(worker, res)
        with pytest.raises(Rejected, match='^robots_disallowed$'):
            gate.request(root + '/public/private')
        gate.request(root + '/public/one')
        gate.request(root + '/public/two')
        writes = pinned.return_value.request.call_args_list
        assert [c.args[1] for c in writes] == ['/robots.txt', '/public/one', '/public/two']
        assert all(c.kwargs['headers']['User-Agent'] == network_gate.USER_AGENT for c in writes)
        assert [c.args[0] for c in resolve.call_args_list] == [host] * 3
        assert [tuple(c.args[:2]) for c in pinned.call_args_list] == [(host, '93.184.216.34')] * 3
        assert [row[0] - now for row in store.db.execute('SELECT started FROM requests ORDER BY rowid')] == [
            0, max(5, delay), 2 * max(5, delay)]
        assert store.db.execute("SELECT reason FROM audit WHERE operation='news_robots'").fetchone()[0] == 'robots_disallowed'


def test_linux_non_news_ignores_spacing_and_review_snapshot(tmp_path, monkeypatch):
    url = 'https://spa.example.test/public'
    cfg = load()
    cfg['policies'] = [{'origin': 'https://spa.example.test', 'public': True, 'paths': ['/public'],
                        'disallow': ['/',], 'crawl_delay': 20, 'robots_reviewed_at': '2020-01-01'}]
    now = datetime(2026, 11, 25, tzinfo=timezone.utc).timestamp()
    assert policy_for(cfg, url, now=now)
    with closing(Store(tmp_path / 'linux.sqlite3')) as store:
        worker = Worker(store, cfg, lambda: now)
        res = worker.begin(worker.reserve(uid(), url, uid(), 'auto_discovery'))
        store.db.execute('UPDATE domains SET next_at=?', (now + 3600,))
        pinned = Mock()
        pinned.return_value.getresponse.side_effect = [MemoryResponse(), MemoryResponse()]
        monkeypatch.setattr(network_gate, 'public_addresses', Mock(return_value=['93.184.216.34']))
        monkeypatch.setattr(network_gate, 'PinnedTLS', pinned)
        gate = network_gate.NetworkGate(worker, res)
        gate.request(url + '/one')
        gate.request(url + '/two')
        writes = pinned.return_value.request.call_args_list
        assert [c.args[1] for c in writes] == ['/public/one', '/public/two']
        assert all(c.kwargs['headers']['User-Agent'] == network_gate.USER_AGENT for c in writes)
        assert [row[0] for row in store.db.execute('SELECT started FROM requests')] == [now, now]
        assert store.db.execute('SELECT next_at FROM domains').fetchone()[0] == now
