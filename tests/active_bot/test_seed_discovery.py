"""Offline producer, identity, transport and web-handoff regression coverage."""
from collections import Counter
from contextlib import closing
from dataclasses import replace
import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from hanset_active import cli, web_proxy
from hanset_active.collect import service, targets
from hanset_active.collect.plan import assemble_targets, run_plan
from hanset_active.collect.store import connect as claim_connect
from hanset_active.config import Settings
from hanset_active.crawl_policy import COLLECTION, DISCOVERY, is_korean_news
from hanset_active.db import connect
from hanset_active.discover import datago, seeds
from hanset_active.discover.candidates import Candidates, Observation
from hanset_active.discover.search import Hit, SearchProvider, Tavily
from hanset_active.fetcher import Fetcher, USER_AGENT
from hanset_active.locks import RunLock
from hanset_active.search import budget, tavily
from hanset_active.util.timeutil import FakeClock
from hanset_active.venues import seed
from tests.active_bot.test_collect_deepseek import HOURS, enable_qwen
from tests.active_bot.test_collect_plan import env
from tests.active_bot.test_collect_unchanged import Model
from tests.active_bot.test_tavily import Response

_BATHS = datago.HOST + '/public_baths/info'
_GYMS = datago.HOST + '/fitness_centers/info'
_TEST_CREDENTIAL = 'fixture-license-marker'
_PRIVATE_FIELD = 'fixture-private-field'
_ADDRESS = '서울특별시 강남구 예시로 10-12'
_OWN = 'https://seedspa.test/sauna'
_BLOG = 'https://blog.naver.com/seed-spa-a'
_OTA = 'https://booking.com/seed-spa-a'


@pytest.fixture
def state(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / 'active', testing=True, min_interval=0, global_gap=0)
    clock = FakeClock()
    monkeypatch.setenv('HANSET_DATAGO_KEY_FILE', str(tmp_path / 'license-key'))
    with closing(connect(settings.data_dir / 'active.sqlite3')) as db:
        yield settings, clock, db


def response(status=200, body=b'', content_type='text/html', **headers):
    if isinstance(body, str):
        body = body.encode()
    if isinstance(body, dict):
        body = json.dumps(body, ensure_ascii=False).encode()
        content_type = 'application/json'
    return SimpleNamespace(status_code=status, headers={'Content-Type': content_type, **headers},
                           iter_content=lambda size: iter([body]), close=lambda: None)


def transport(monkeypatch, routes):
    calls = []

    def get(session, url, **kwargs):
        assert session.headers['User-Agent'] == USER_AGENT
        assert not session.trust_env and kwargs['allow_redirects'] is False
        calls.append(url)
        return routes(url) if callable(routes) else routes.get(url, response(404))

    monkeypatch.setattr(requests.Session, 'get', get)
    return calls


def license_key(tmp_path, mode=0o600):
    path = tmp_path / 'license-key'
    path.write_text(_TEST_CREDENTIAL)
    path.chmod(mode)


def license_row(index=1, name='Seed Spa A', status='01', **fields):
    return {'MNG_NO': f'row-{index}', 'BPLC_NM': name, 'ROAD_NM_ADDR': _ADDRESS,
            'SALS_STTS_CD': status, 'DAT_UPDT_PNT': '2026-01-01 12:01:00',
            'LCPMT_YMD': '20260101', 'TELNO': _PRIVATE_FIELD,
            'OWNER_NAME': _PRIVATE_FIELD, 'MEMO': _TEST_CREDENTIAL,
            'CRD_INFO_X': '12345.125', 'CRD_INFO_Y': '54321.125', **fields}


def license_page(page, total, rows):
    return response(body={'response': {'header': {'resultCode': '00'}, 'body': {
        'pageNo': page, 'totalCount': total, 'items': rows}}})


def invoke(state, sources, **kwargs):
    settings, clock, db = state
    return seeds.discover(settings, db, sources=sources, clock=clock, **kwargs)


def candidate_state(db):
    return [(table, [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY 1')])
            for table in ('seed_candidates', 'seed_candidate_sources', 'seed_venues')]


@pytest.mark.parametrize('mode,status', [(None, 'skipped:no_key'), (0o644, 'skipped:unsafe_key')])
def test_cli_skips_license_credentials_without_dispatch(state, tmp_path, monkeypatch, capsys, mode, status):
    settings, _, _ = state
    if mode is not None:
        license_key(tmp_path, mode)
    calls = transport(monkeypatch, {})
    assert cli.main(['--data-dir', str(settings.data_dir), 'seeds', 'discover', '--source', 'datago']) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and json.loads(lines[0])['sources']['licenses']['status'] == status
    assert calls == []


def test_license_resume_inclusive_replay_and_privacy(state, tmp_path, monkeypatch, capsys):
    settings, clock, db = state
    license_key(tmp_path)
    frozen_id = seed(db, [{'name': 'Box B', 'website': 'https://other.example.test/'}], clock=clock)
    fail = True
    first_page = [license_row()] + [license_row(i, 'Closed Spa', '03') for i in range(2, 101)]

    def routes(url):
        query = parse_qs(urlsplit(url).query)
        assert query['serviceKey'] == [_TEST_CREDENTIAL] and query['returnType'] == ['json']
        assert query['numOfRows'] == ['100'] and query['cond[SALS_STTS_CD::EQ]'] == ['01']
        page = int(query['pageNo'][0])
        if url.startswith(_GYMS):
            return license_page(1, 2, [license_row(1, 'CrossFit Box B'), license_row(2, 'Off Topic')])
        if 'cond[DAT_UPDT_PNT::GTE]' in query:
            assert query['cond[DAT_UPDT_PNT::GTE]'] == ['2026-01-01 12:01:00']
            return license_page(1, 2, [license_row(), license_row(101, 'Seed Spa C', ROAD_NM_ADDR='서울 강남구 예시로 20')])
        if page == 1:
            return license_page(1, 101, first_page)
        if fail:
            return response(503)
        return license_page(2, 101, [license_row(101, 'Seed Spa C', ROAD_NM_ADDR='서울 강남구 예시로 20')])

    calls = transport(monkeypatch, routes)
    first = invoke(state, ['licenses'])
    assert first['new'] == 1 and first['sources']['licenses']['status'] == 'partial:license_request'
    fail = False
    start = len(calls)
    second = invoke(state, ['licenses'])
    assert second['new'] == 2 and parse_qs(urlsplit(calls[start]).query)['pageNo'] == ['2']
    before = candidate_state(db)
    clock.sleep(7200)
    replay = invoke(state, ['licenses'])
    assert all(replay[key] == 0 for key in seeds.TRANSITIONS)
    assert candidate_state(db) == before
    assert {row[0] for row in db.execute('SELECT name FROM seed_candidates')} == {'Seed Spa A', 'Seed Spa C', 'CrossFit Box B'}
    assert db.execute("SELECT cursor FROM run_checkpoints WHERE checkpoint_id='seed_freeze'").fetchone()[0] == frozen_id
    fields = json.loads(db.execute('SELECT fields_json FROM seed_candidate_sources LIMIT 1').fetchone()[0])
    assert fields['coordinates']['crs'] == 'EPSG:5174' and fields['license_date'] == '2026-01-01'
    assert not db.execute('SELECT 1 FROM budgets_daily').fetchone()
    captured = capsys.readouterr()
    for path in settings.data_dir.glob('active.sqlite3*'):
        payload = path.read_bytes()
        for private in (_TEST_CREDENTIAL, _PRIVATE_FIELD, 'TELNO', 'OWNER_NAME', 'serviceKey'):
            assert private.encode() not in payload
            assert private not in captured.out + captured.err


def test_discovery_goes_past_legacy_aux_limit_and_auth_redirect_stops(state, tmp_path, monkeypatch):
    license_key(tmp_path)
    settings, clock, db = state

    def routes(url):
        page = int(parse_qs(urlsplit(url).query)['pageNo'][0])
        if url.startswith(_GYMS):
            return response(302, Location='https://unsafe.example.test')
        # Real-sized pages exercise the upper bound without retaining raw input.
        rows = [license_row(page * 100 + i, 'Closed Spa', '03') for i in range(100)]
        return license_page(page, 80100, rows)

    calls = transport(monkeypatch, routes)
    result = invoke(state, ['licenses'])
    assert len(calls) == 802 and result['sources']['licenses']['status'] == 'partial:license_request'
    assert db.execute('SELECT count(*) FROM request_log').fetchone()[0] == 802
    assert not db.execute('SELECT 1 FROM budgets_daily').fetchone()
    assert db.execute('SELECT reason FROM failures').fetchone()[0] == 'error'
    assert not any('unsafe.example.test' in call for call in calls)
    legacy = Fetcher(replace(settings, aux_cap=0), db, clock)
    try:
        assert legacy.fetch('https://legacy.example.test/', kind='api').outcome == 'cap_reached'
        assert len(calls) == 802
    finally:
        legacy.close()


def page(name, address='', *, raw='', kind='DaySpa'):
    data = {'@type': kind, 'name': name, 'address': address}
    return response(body=f'<title>{name}</title><main><h1>{name}</h1>{raw}{HOURS}'
                         f'<script type="application/ld+json">{json.dumps(data, ensure_ascii=False)}</script></main>')


@pytest.mark.parametrize('address', [_ADDRESS, ''])
def test_license_and_known_sitemap_attach_to_one_identity(state, tmp_path, monkeypatch, address):
    settings, clock, db = state
    license_key(tmp_path)
    approved = tmp_path / 'origins.json'
    approved.write_text(json.dumps(['https://seedspa.test/']))
    seed(db, approved_sources=approved, clock=clock)
    index = '<sitemapindex><sitemap><loc>https://seedspa.test/areas.xml</loc></sitemap></sitemapindex>'
    sitemap_body = f'<urlset><url><loc>{_OWN}</loc></url></urlset>'

    def routes(url):
        if url.startswith(_BATHS):
            return license_page(1, 1, [license_row()])
        if url.startswith(_GYMS):
            return license_page(1, 0, [])
        return {'https://seedspa.test/robots.txt': response(body='User-agent: *\nDisallow: /', content_type='text/plain'),
                'https://seedspa.test/sitemap.xml': response(body=index, content_type='application/xml'),
                'https://seedspa.test/areas.xml': response(body=sitemap_body, content_type='application/xml'),
                _OWN: page('Seed Spa A', address)}.get(url, response(404))

    transport(monkeypatch, routes)
    result = invoke(state, ['licenses', 'sitemaps'])
    assert result['new'] == result['resolved'] == result['promoted'] == result['duplicate'] == 1
    row, = db.execute('SELECT * FROM seed_candidates')
    assert row['name'] == 'Seed Spa A' and row['official_domain'] == 'seedspa.test'
    assert {tuple(row) for row in db.execute('SELECT source_kind, fetch_url FROM seed_candidate_sources')} == {
        ('licenses', None), ('sitemaps', _OWN)}
    before = candidate_state(db)
    clock.sleep(7200)
    replay = invoke(state, ['licenses', 'sitemaps'])
    assert all(replay[key] == 0 for key in seeds.TRANSITIONS) and candidate_state(db) == before


def test_alias_hotel_branches_http_only_and_listicle(state):
    settings, clock, db = state
    store = Candidates(db, clock, profile=settings.profile)
    records = [
        Observation('Seed Spa A', 'spa', 'licenses', _BATHS, _ADDRESS),
        Observation('Seed Spa A', 'spa', 'sitemaps', _OWN, fetch_url=_OWN, ownership='venue_page_on_known_site', single_venue=True),
        Observation('Seed Spa Alias', 'spa', 'sitemaps', 'https://seedspa.test/wellness',
                    fetch_url='https://seedspa.test/wellness', ownership='first_party_business', single_venue=True),
        Observation('Hotel Spa Branch', 'hotel_spa', 'sitemaps', 'https://hotel.example/one',
                    '서울 예시로 2', fetch_url='https://hotel.example/one', ownership='venue_name_and_address'),
        Observation('Hotel Spa Branch', 'hotel_spa', 'sitemaps', 'https://hotel.example/two',
                    '서울 예시로 3', fetch_url='https://hotel.example/two', ownership='venue_name_and_address'),
        Observation('Box B', 'gym', 'directories', 'http://box.example.test', fetch_url='http://box.example.test'),
        Observation('Top 5 Spa', 'spa', 'tavily', _BLOG, fetch_url=_BLOG),
    ]
    counts = Counter()
    with db:
        for item in records:
            counts.update(store.record(item))
    assert counts == {'new': 4, 'duplicate': 2, 'resolved': 3, 'promoted': 3, 'rejected': 1}
    rows = list(db.execute('SELECT name, status, reject_reason FROM seed_candidates'))
    assert [tuple(r) for r in rows].count(('Hotel Spa Branch', 'promoted', None)) == 2
    assert ('Box B', 'new', None) in map(tuple, rows)
    assert ('Top 5 Spa', 'rejected', 'article_title') in map(tuple, rows)
    assert db.execute("SELECT count(*) FROM seed_candidate_sources WHERE fetch_url LIKE 'http:%'").fetchone()[0] == 0


def test_tavily_resolves_public_sources_and_respects_quota(state, monkeypatch):
    settings, clock, db = state
    monkeypatch.setenv('TAVILY_API_KEY', 'fixture-tavily-marker')
    store = Candidates(db, clock, profile=settings.profile)
    with db:
        store.record(Observation('Seed Spa A', 'spa', 'licenses', _BATHS))
    results = [{'url': _BLOG}, {'url': _OTA}, {'url': 'https://runport.example.test/'}]
    calls = transport(monkeypatch, {_BLOG: page('Seed Spa A'), _OTA: page('Seed Spa A'),
                                    'https://runport.example.test/': page('RUNPORT Box B', '서울', kind='ExerciseGym')})
    queries = []

    def post(url, **kwargs):
        assert url == tavily.ENDPOINT and kwargs['headers']['User-Agent'] == USER_AGENT
        queries.append(kwargs['json']['query'])
        return Response({'results': results, 'usage': {'credits': 1}})

    first = invoke(state, ['tavily'], max_calls=1, tavily_transport=post)
    assert first['promoted'] == 1 and first['sources']['tavily']['calls'] == 1
    assert set(calls) == {_BLOG, _OTA, 'https://runport.example.test/'}
    assert db.execute('SELECT official_domain FROM seed_candidates').fetchone()[0] is None
    assert {tuple(r) for r in db.execute('SELECT label_status,verification FROM seed_candidate_sources')} == {('source_claim', '미확인')}
    assert 'Seed Spa A' in queries[0]
    # The one-call rotation next enters nationwide discovery.
    second = invoke(state, ['tavily'], max_calls=1, tavily_transport=post)
    assert second['sources']['tavily']['calls'] == 1 and '서울' in queries[1]
    assert db.execute("SELECT official_domain FROM seed_candidates WHERE name='RUNPORT Box B'").fetchone()[0] == 'example.test'
    assert budget.availability(clock=clock).snapshot.reported_credits == 2

    quota_calls = []

    def quota(url, **kwargs):
        quota_calls.append(kwargs['json']['query'])
        return Response(status=432)

    with db:
        store.record(Observation('Box B', 'crossfit_box', 'licenses', _GYMS))
    result = invoke(state, ['tavily', 'licenses'], tavily_transport=quota)
    assert result['sources']['licenses']['status'] == 'skipped:no_key'
    assert result['sources']['tavily']['status'] == 'tavily_provider_quota' and len(quota_calls) == 1
    assert db.execute("SELECT count(*) FROM request_log WHERE outcome='tavily_provider_quota'").fetchone()[0] == 1
    assert budget.availability(clock=clock).snapshot.used == 3
    assert invoke(state, ['tavily'], max_calls=0, tavily_transport=quota)['sources']['tavily']['calls'] == 0


def test_public_search_policy_keeps_naver_and_runport(state, monkeypatch):
    _, clock, db = state
    monkeypatch.setenv('TAVILY_API_KEY', 'fixture-tavily-marker')
    posts = []

    def post(url, **kwargs):
        posts.append(url)
        return Response({'results': [{'url': _BLOG}, {'url': 'https://runport.example.test/'},
                                     {'url': 'https://instagram.com/venue/'}]})

    provider = Tavily(None, db, clock=clock, public_source=True, transport=post)
    assert {hit.url.rstrip('/') for hit in provider.search('런포트 사우나')} == {_BLOG, 'https://runport.example.test'}
    assert Tavily(None, db, clock=clock, transport=post).search('런포트 사우나') == []
    assert len(posts) == 1

    class Generic(SearchProvider):
        endpoint = 'https://search.example.test/'
        name = 'fixture'

        def parse(self, result, limit):
            return [Hit(_BLOG), Hit('https://runport.example.test'), Hit('https://127.0.0.1')]

    generic = Generic(lambda *_, **__: SimpleNamespace(ok=True), db, clock=clock, public_source=True)
    assert len(generic.search('런포트 사우나')) == 2


def test_news_only_robots_spacing_terminal_block_and_direct_fallback(state, monkeypatch):
    settings, clock, db = state
    assert not is_korean_news(_BLOG) and is_korean_news('https://n.news.naver.com/item')
    news = 'https://news.naver.com'
    events = []

    def routes(url):
        events.append((url, clock.monotonic()))
        if url.endswith('/robots.txt'):
            return response(body='User-agent: *\nDisallow: /private\nCrawl-delay: 2.5', content_type='text/plain')
        if url.endswith('/blocked'):
            return response(403)
        return page('Seed Spa A')

    calls = transport(monkeypatch, routes)
    fetcher = Fetcher(settings, db, clock, public_scope=True, policy=DISCOVERY)
    try:
        assert fetcher.fetch(_BLOG).ok
        assert fetcher.fetch(_BLOG + '/other').ok
        assert fetcher.fetch(news + '/page').ok
        assert fetcher.fetch(news + '/page2').ok
        assert fetcher.fetch(news + '/private').outcome == 'robots_disallowed'
        assert fetcher.fetch(_BLOG + '/after-news').ok
        assert fetcher.fetch('https://instagram.com/venue').outcome == 'denied'
        assert fetcher.fetch('https://block.example/blocked').outcome == 'blocked'
        assert fetcher.fetch('https://block.example/blocked').reason == 'resource_hold'
        assert fetcher.fetch('https://block.example/later').ok
        assert events[:2] == [(_BLOG, 0), (_BLOG + '/other', 0)]
        assert events[2:5] == [(news + '/robots.txt', 0), (news + '/page', 5), (news + '/page2', 10)]
        assert news + '/private' not in calls and 'https://block.example/later' in calls
    finally:
        fetcher.close()

    monkeypatch.setattr(web_proxy, 'reachable', lambda proxy: False)
    seen = []

    def get(session, url, **kwargs):
        seen.append(bool(session.proxies))
        if session.proxies:
            raise requests.ConnectionError('fixture')
        return page('Seed Spa A')

    monkeypatch.setattr(requests.Session, 'get', get)
    fetcher = Fetcher(settings, db, clock, proxy='socks5h://127.0.0.1:1080', public_scope=True, policy=DISCOVERY)
    try:
        assert fetcher.fetch('https://outage.example.test/sauna').ok and seen == [True, False]
    finally:
        fetcher.close()


def test_overlapping_discovery_and_collector_locks_are_independent(state):
    settings, clock, db = state
    with RunLock(db, settings.data_dir, 'discovery', clock=clock, filename='seeds-discover.lock', lease_key='seed_discovery'):
        with RunLock(db, settings.data_dir, 'collector', clock=clock):
            assert invoke(state, ['licenses'])['state'] == 'already_running'
            assert {tuple(r) for r in db.execute('SELECT lease_key,run_id FROM run_lease')} == {
                ('collector', 'collector'), ('seed_discovery', 'discovery')}


def _process_fetch(directory, entered, release, events, worker):
    settings = Settings(data_dir=Path(directory), testing=True, min_interval=0, global_gap=0)
    with closing(connect(settings.data_dir / 'active.sqlite3')) as db:
        fetcher = Fetcher(settings, db, FakeClock(), public_scope=True, policy=COLLECTION)

        def get(url, **_):
            events.put((worker, 'start', url))
            if worker == 'first' and url.endswith('/robots.txt'):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('fixture_timeout')
            events.put((worker, 'end', url))
            if url.endswith('/entry'):
                return response(302, Location='/sauna')
            if url.endswith('/robots.txt'):
                return response(body='User-agent: *\nAllow: /', content_type='text/plain')
            return page('Seed Spa A', raw='Hours and details for visitors. ' * 5)

        fetcher.session.get = get
        try:
            assert fetcher.fetch('https://news.naver.com/' + ('entry' if worker == 'first' else 'other')).ok
        finally:
            fetcher.close()


def test_cross_process_domain_flock_serializes_robots_and_redirects(state):
    settings, _, _ = state
    ctx = multiprocessing.get_context('fork')
    entered, release, events = ctx.Event(), ctx.Event(), ctx.Queue()
    first = ctx.Process(target=_process_fetch, args=(settings.data_dir, entered, release, events, 'first'))
    second = ctx.Process(target=_process_fetch, args=(settings.data_dir, entered, release, events, 'second'))
    first.start()
    try:
        assert entered.wait(5)
        second.start()
        assert events.get(timeout=5)[1] == 'start'
        from queue import Empty
        with pytest.raises(Empty):
            events.get(timeout=0.15)
        release.set()
        first.join(5)
        second.join(5)
        assert first.exitcode == second.exitcode == 0
        assert [events.get(timeout=3)[1] for _ in range(7)] == ['end', 'start', 'end', 'start', 'end', 'start', 'end']
    finally:
        release.set()
        for process in (first, second):
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join()
        events.close()


def test_web_collect_consumes_post_freeze_backlog_and_rotates(env, monkeypatch):
    settings, clock, db = env
    enable_qwen(monkeypatch)
    active = Settings(data_dir=settings.active_dir, testing=True, min_interval=0, global_gap=0)
    assert run_plan(settings, clock=clock, testing=True, ranking='local')['state'] == 'done'
    plan_path = next((settings.data_dir / 'plans').glob('*.json'))
    frozen = plan_path.read_bytes()
    store = Candidates(db, clock, profile=active.profile)
    with db:
        for index in range(105):
            url = f'https://seed-{index}.example.test/sauna'
            store.record(Observation(f'Seed Spa {index}', 'spa', 'tavily', url, fetch_url=url))
    assembled = assemble_targets(settings, now=clock.now(), testing=True)
    assert len(assembled['targets']) == 100
    assert len(assembled['authorization']) == len(service._change_targets(settings, testing=True, clock=clock)) == 105
    fixed = {targets.target_fingerprint(row) for row in assembled['authorization']}
    planned_urls = {url for row in assembled['targets'] for url in row['urls']}
    calls = transport(monkeypatch, lambda url: page('Seed Spa B', raw=' Details for visitors. ' * 10))
    result = service.run_web(settings, clock=clock, qwen_session=Model(), testing=True)
    assert result['success'] and len(calls) == 105
    assert plan_path.read_bytes() == frozen
    assert len(set(calls)) == 105 and set(calls) - planned_urls
    clock.sleep(86400)
    model = Model()
    resumed = service.run_web(settings, clock=clock, qwen_session=model, testing=True)
    assert resumed['counts']['unchanged'] == 105 and not model.calls
    after = service._change_targets(settings, testing=True, clock=clock)
    assert {targets.target_fingerprint(row) for row in after} == fixed
    with closing(claim_connect(settings.data_dir)) as receipts:
        assert {tuple(r) for r in receipts.execute('SELECT DISTINCT source_type,status,rights FROM claims')} == {
            ('third_party', 'source_claim', 'internal_review_only')}


def test_public_directories_fetch_korean_detail_and_verify_labelled_homepage(state, monkeypatch):
    _, _, db = state
    crossfit = 'https://www.crossfit.com'
    hyrox = 'https://hyrox.com'
    detail = crossfit + '/affiliate/box-b'
    foreign = crossfit + '/affiliate/box-c'
    owned = 'https://boxb.test/'
    race = hyrox + '/event/fixture-seoul'
    routes = {
        crossfit + '/map': response(body=f'<main><a href="{detail}">CrossFit Box B</a>'
                                        f'<a href="{foreign}">CrossFit Box C</a></main>'),
        detail: response(body='<main><h1>CrossFit Box B</h1><address>Seoul</address>'
                              f'<p>Website <a href="{owned}">Box B</a></p></main>'),
        foreign: page('CrossFit Box C', 'Berlin', kind='ExerciseGym'),
        owned: page('CrossFit Box B', 'Seoul', kind='ExerciseGym'),
        hyrox + '/find-my-race/': response(body=f'<main><a href="{race}">Seoul</a></main>'),
        race: page('HYROX Seoul Event', 'Seoul', kind='SportsEvent'),
    }
    calls = transport(monkeypatch, routes)
    result = invoke(state, ['directories'])
    assert result['promoted'] == 2 and owned in calls and foreign in calls
    candidates = list(db.execute('SELECT name,category,official_domain FROM seed_candidates ORDER BY name'))
    assert list(map(tuple, candidates)) == [('CrossFit Box B', 'crossfit_box', 'boxb.test'),
                                          ('HYROX Seoul Event', 'wellness_event', None)]
    assert {tuple(row) for row in db.execute('SELECT fetch_url,official FROM seed_candidate_sources')} == {
        (detail, 0), (owned, 1), (race, 0)}


def test_promoted_targets_manual_suppression_and_slash_handoff(env, monkeypatch, tmp_path):
    settings, clock, db = env
    enable_qwen(monkeypatch)
    active = Settings(data_dir=settings.active_dir)
    good = 'https://venue.example.test/sauna/'
    suppressed = 'https://unlisted.example.test/venue'
    store = Candidates(db, clock, profile=active.profile)
    with db:
        store.record(Observation('Seed Spa A', 'spa', 'tavily', good, fetch_url=good))
        store.record(Observation('Box B', 'gym', 'tavily', suppressed, fetch_url=suppressed))
    authorization = assemble_targets(settings, now=clock.now(), testing=True)['authorization']
    disabled = next(target for target in authorization if target['name'] == 'Box B')
    manual = settings.data_dir / 'web_targets.json'
    manual.write_text(json.dumps({'schema_version': 1, 'targets': [{
        'enabled': False, 'venue_id': disabled['venue_id'], 'venue_key': disabled['venue_key']}]}))
    calls = transport(monkeypatch, {good: page('Seed Spa A', raw='Visitor information. ' * 8)})
    result = service.run_web(settings, clock=clock, qwen_session=Model(), testing=True)
    assert result['success'] and calls == [good]
    override = tmp_path / 'explicit.json'
    url = 'https://other.test/sauna'
    override.write_text(json.dumps({'schema_version': 1, 'targets': [
        {'venue_id': 'explicit', 'venue_key': 'explicit', 'name': 'Spa C', 'official_urls': [url]}
    ]}))
    assert service.run_web(settings, targets_path=override, clock=clock, qwen_session=Model(), testing=True)['success']
    assert calls == [good, url]


def test_known_venue_public_source_policy_leaves_gap_unverified(state, monkeypatch):
    from hanset_active.gaps import ensure_gaps
    from hanset_active.search import discover_known_venue
    from hanset_active.venues import upsert_venue

    _, clock, db = state
    monkeypatch.setenv('TAVILY_API_KEY', 'fixture-tavily-marker')
    vid = upsert_venue(db, 'Seed Spa A', city='Seoul')
    ensure_gaps(db, vid, clock=clock)
    with db:
        db.execute('UPDATE venues SET name=?, website=? WHERE venue_id=?', ('런포트 Box B', _BLOG, vid))
    outcome = discover_known_venue(db, vid, 'price_day_pass', clock=clock, public_source=True,
                                  transport=lambda *a, **k: Response({'results': [{'url': _BLOG}]}))
    assert len(outcome.leads) == 1 and outcome.leads[0].url == _BLOG
    assert outcome.leads[0].verification == 'unverified'
    assert discover_known_venue(db, vid, 'price_day_pass', clock=clock).reason == 'tavily_venue_excluded'


def test_declined_listing_keeps_official_evidence_internal(env, monkeypatch):
    settings, clock, db = env
    enable_qwen(monkeypatch)
    url = 'https://runport.example.test/sauna'
    store = Candidates(db, clock, profile=Settings(data_dir=settings.active_dir).profile)
    with db:
        store.record(Observation('RUNPORT Box B', 'gym', 'tavily', url, fetch_url=url,
                                 ownership='venue_name_and_host', single_venue=True))
    target, = assemble_targets(settings, now=clock.now(), testing=True)['authorization']
    assert target['official_urls'] == [url]
    calls = transport(monkeypatch, {url: page('RUNPORT Box B', raw='Details for visitors. ' * 8)})
    assert service.run_web(settings, clock=clock, qwen_session=Model(), testing=True)['collected']
    assert calls == [url]
    with closing(claim_connect(settings.data_dir)) as receipts:
        assert [tuple(row) for row in receipts.execute('SELECT source_type,rights FROM claims')] == [
            ('own_site', 'internal_review_only')]
        assert not receipts.execute('SELECT 1 FROM web_change_observations').fetchone()
    assert not (settings.data_dir / 'watchlist-proposals.json').exists()
