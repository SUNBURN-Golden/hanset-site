"""Public seed collection through the production freeze and guarded web paths."""
import json
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest
import requests

from hanset_active.collect import service, targets
from hanset_active.collect.plan import order_targets, run_plan, validate_plan
from hanset_active.collect.store import connect, digest, web_target_history
from hanset_active.fetcher import Fetcher, FetchResult, USER_AGENT
from hanset_active.venues import seed, upsert_venue
from tests.active_bot.test_collect_deepseek import HOURS, KEY
from tests.active_bot.test_collect_plan import env, save_targets
from tests.active_bot.test_collect_promotion import approval, policy
from tests.active_bot.test_collect_subpages import html
from tests.active_bot.test_collect_targets import assembly
from tests.active_bot.test_collect_unchanged import DAY, Model
from tests.active_bot.test_web_proxy import http_response


NAVER = 'https://blog.naver.com/seed-spa-a'
NAVER_PORTAL = 'https://naver.com/seed-spa-a'
OWN = 'https://spa.example.test/hours'
DECLINED = 'https://runport.example.test/hours'
OTA = 'https://booking.test/seed-spa-a'
MAP = 'https://maps.test/seed-spa-a'
INSTAGRAM = 'https://instagram.com/seed.spa/'


def row(name, website):
    return {'name': name, 'website': website}


def fake_http(monkeypatch, routes):
    calls = []

    def get(session, url, **kwargs):
        assert session.headers['User-Agent'] == USER_AGENT and not session.trust_env
        assert kwargs['allow_redirects'] is False
        calls.append(url)
        return http_response(404) if urlsplit(url).path == '/robots.txt' else routes[url]

    monkeypatch.setattr(requests.Session, 'get', get)
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    monkeypatch.delenv('HANSET_WEB_PROXY', raising=False)
    return calls


@pytest.fixture
def shared_domains(monkeypatch):
    from hanset_active.collect import config

    profile = config.load_profile()
    profile['reservation_platform_domains'].append('booking.test')
    profile['aggregator_domains'].append('maps.test')
    monkeypatch.setattr(config, 'load_profile', lambda: profile)
    monkeypatch.setattr(targets, 'load_profile', lambda: profile)


def test_freeze_admission_and_full_plan_replay(env, tmp_path, shared_domains):
    settings, clock, db = env
    urls = [f'{OWN}/{n}' for n in range(5)]
    assert seed(db, [row('Box B', OWN)], clock=clock)
    before = list(db.iterdump())
    inputs = [row('Seed Spa A', url) for url in [*urls, urls[0] + '/', NAVER, OTA, MAP]]
    inputs += [row('RUNPORT Box B', DECLINED), row('Seed Spa A 후기', 'https://article.test'),
               row('Box B', INSTAGRAM), row('Box B', 'https://instagr.am/seed')]
    approved = tmp_path / 'seed-domains.json'
    approved.write_text(json.dumps(['https://naver.com/', 'https://runport.example.test', INSTAGRAM]))
    assert seed(db, inputs, approved_sources=approved, clock=clock)
    assert list(db.iterdump()) == before
    freeze = seed(db, inputs, approved_sources=approved, refreeze=True, clock=clock)
    frozen = db.execute('SELECT name,website FROM seed_venues WHERE seed_id=?', (freeze,)).fetchall()
    assert {r['website'] for r in frozen} == {*urls, NAVER, OTA, MAP, DECLINED}
    assert {r[0] for r in db.execute('SELECT domain FROM seed_domains WHERE seed_id=?', (freeze,))} == {
        'naver.com', 'example.test', 'booking.test', 'maps.test',
    }
    assert upsert_venue(db, 'RUNPORT Box B', website=DECLINED) is None
    assert upsert_venue(db, 'Seed Spa A', website=NAVER) is None
    by_url = {url: target for target in assembly(env)['targets'] for url in target['urls']}
    assert by_url[NAVER]['venue_id'].startswith('seed:') and by_url[DECLINED]['venue_id'].startswith('seed:')
    assert not db.execute('SELECT 1 FROM venues').fetchone()
    for url in (NAVER, OTA, MAP, DECLINED):
        assert by_url[url]['official_urls'] == [] and by_url[url]['domains'] == set()
        assert targets.source_authorization(by_url[url], url) is None
    assert set(by_url[urls[0]]['urls']) == set(urls)
    first = run_plan(settings, clock=clock, testing=True, ranking='local')
    plan_path = settings.data_dir / 'plans' / (first['day'] + '.json')
    stored = plan_path.read_bytes()
    plan = json.loads(stored)
    assert validate_plan(plan, first['day'])
    assert any(set(item['urls']) == set(urls) for item in plan['web_targets'])
    run_plan(settings, clock=clock, testing=True, ranking='local')
    assert plan_path.read_bytes() == stored
    ordered, _ = order_targets(assembly(env)['targets'], plan)
    jobs, _ = targets.resolve_jobs(ordered, search=None, db=None, clock=clock, testing=True)
    assert {job['url'] for job in jobs} == set(by_url)
    assert {job['url'] for job in jobs if job['source_authorization']} == set(urls)


def test_manual_authority_and_baselines_survive_third_party_seed(env, monkeypatch, shared_domains):
    settings, clock, db = env
    vid = upsert_venue(db, 'Seed Spa A', website=OWN, clock=clock)
    key = db.execute('SELECT venue_key FROM venues WHERE venue_id=?', (vid,)).fetchone()[0]
    urls = [OWN, *(f'{OWN}/{n}' for n in range(4))]
    manual = {'venue_id': vid, 'venue_key': key, 'name': 'Seed Spa A',
              'urls': urls, 'domains': {'example.test'}, 'tavily': None}
    save_targets(settings.data_dir / 'web_targets.json', [manual])
    seed(db, [row('Box B', OWN + '/')], clock=clock)
    baseline_target = assembly(env)['targets'][0]
    assert baseline_target['official_urls'] == urls
    calls = fake_http(monkeypatch, {url: http_response(200, html(text=HOURS + ' ' + 'Guest information. ' * 9))
                                  for url in urls})
    assert service.run_web(settings, clock=clock, session=Model(), testing=True)['collected']
    with closing(connect(settings.data_dir)) as receipts:
        baselines = receipts.execute('SELECT * FROM web_change_baselines ORDER BY source_identity').fetchall()
        assert len(baselines) == len(urls)
    seed(db, [row('Seed Spa A', NAVER), row('Seed Spa A', OTA), row('Box B', OWN + '/')], refreeze=True, clock=clock)
    target = next(t for t in assembly(env)['targets'] if t['venue_id'] == vid)
    assert set(target['urls']) == {*urls, NAVER, OTA}
    assert target['domains'] == {'example.test'} and target['official_urls'] == urls
    assert target['scope_domains'] == {'example.test', 'naver.com', 'booking.test'}
    assert targets.target_fingerprint(target) == targets.target_fingerprint(baseline_target)
    for url in (NAVER, OTA):
        assert targets.source_authorization(target, url) is None
    third_party_calls = fake_http(monkeypatch, {url: http_response(200, html(text=HOURS + ' ' + filler * 9))
                                               for url, filler in [(NAVER, 'Spa review. '), (OTA, 'Booking details. ')]})
    result = service.run_web(settings, clock=clock, session=Model(), testing=True)
    assert result['collected']
    assert calls.count(OWN) == 1
    assert OWN not in third_party_calls
    with closing(connect(settings.data_dir)) as receipts:
        assert receipts.execute('SELECT * FROM web_change_baselines ORDER BY source_identity').fetchall() == baselines
        assert receipts.execute('SELECT count(*) FROM web_change_events').fetchone()[0] == 0
        assert receipts.execute('SELECT venue_id FROM claims WHERE source_url=?', (OWN,)).fetchone()[0] == vid
    path = policy(settings, *(approval(url, venue_id=vid, venue_key=key, review_only=True)
                              for url in (NAVER, OTA)))
    report = service.run_report(settings, policy_path=path, clock=clock)
    assert report['auto_publish_to_site'] is False
    claims = {item['source_url']: item for item in report['claims']}
    assert claims[OWN]['source_type'] == 'own_site'
    for url in (NAVER, OTA):
        assert (claims[url]['status'], claims[url]['source_type'], claims[url]['rights'],
                claims[url]['verification_label']) == ('source_claim', 'third_party', 'internal_review_only', '미확인')
    candidate, = report['fact_candidates']
    assert candidate['status'] == 'fact_candidate' and candidate['rights'] == 'internal_review_only'
    assert candidate['verification_label'] == '미확인'
    rationale, = candidate['independence_rationale']
    assert rationale['official_web_required'] and {a['authority'] for a in rationale['source_approvals']} == {'review_only'}


def test_declined_and_blog_pages_yield_claims_without_discovery_or_proposals(env, monkeypatch):
    settings, clock, db = env
    urls = {NAVER, NAVER_PORTAL, DECLINED}
    seed(db, [row('Seed Spa A', NAVER), row('Seed Spa A', NAVER_PORTAL), row('RUNPORT Box B', DECLINED)], clock=clock)
    page = html(f'<a href="/more">Price</a><a href="{INSTAGRAM}">Profile</a>',
                HOURS + ' Transient RUNPORT site marker. ' * 7)
    calls = fake_http(monkeypatch, {url: http_response(200, page) for url in urls})
    result = service.run_web(settings, clock=clock, session=Model(), testing=True)
    assert result['collected']
    assert {url for url in calls if not url.endswith('/robots.txt')} == urls
    assert not (settings.data_dir / 'watchlist-proposals.json').exists()
    with closing(connect(settings.data_dir)) as receipts:
        assert receipts.execute('SELECT count(*) FROM web_change_observations').fetchone()[0] == 0
        assert {r[0] for r in receipts.execute('SELECT source_type FROM claims')} == {'third_party'}
        key = digest(targets.canonical_requested_url(NAVER))
        assert key in web_target_history(receipts, clock=clock)[1]
        clock.sleep(7 * DAY)
        assert key not in web_target_history(receipts, clock=clock)[1]
    for directory in (settings.data_dir, settings.active_dir):
        assert all(b'Transient RUNPORT site marker.' not in p.read_bytes() for p in directory.iterdir() if p.is_file())


def test_legacy_runner_keeps_original_seed_admission_and_first_website(env, monkeypatch):
    from hanset_active import runner

    _, clock, db = env
    seed(db, [row('RUNPORT Box B', 'https://box.test/hours'), row('Box B', NAVER),
              row('Seed Spa A', OWN), row('Seed Spa A', OWN + '/weekend')], clock=clock)
    fetcher = SimpleNamespace(db=db, clock=clock, settings=SimpleNamespace(profile={}),
                              register_candidate=Mock())
    monkeypatch.setattr(runner, 'replenish_search', Mock())
    runner.bootstrap(fetcher)
    fetcher.register_candidate.assert_called_once_with(OWN)


@pytest.mark.parametrize('destination', [INSTAGRAM, 'https://instagr.am/seed',
                                         'https://cdninstagram.com/seed', 'https://other.test/hours'])
def test_seed_redirect_scope_rejects_destination_before_requests(env, monkeypatch, destination):
    settings, clock, db = env
    seed(db, [row('Seed Spa A', NAVER)], clock=clock)
    calls = fake_http(monkeypatch, {NAVER: http_response(302, Location=destination)})
    model = Model()
    result = service.run_web(settings, clock=clock, session=model, testing=True)
    assert result['pages'][-1] == {'url': NAVER, 'disposition': 'denied'} and not model.calls
    assert calls == ['https://blog.naver.com/robots.txt', NAVER]
    if 'instagram.com' in destination or 'instagr.am' in destination:
        with pytest.raises(ValueError):
            targets.canonical_requested_url(destination)
        assert service.classify_page(FetchResult(destination, 'ok'), {targets.registrable_domain(destination)})[0] == 'off_domain'
        fetcher = Fetcher(db=db, public_scope=True)
        assert fetcher.fetch(destination).outcome == 'denied'
        fetcher.close()


def test_naver_block_is_held_for_fourteen_days(env, monkeypatch):
    settings, clock, db = env
    seed(db, [row('Seed Spa A', NAVER)], clock=clock)
    calls = fake_http(monkeypatch, {NAVER: http_response(403)})
    model = Model()
    service.run_web(settings, clock=clock, session=model, testing=True)
    clock.sleep(13 * DAY)
    held = service.run_web(settings, clock=clock, session=model, testing=True)
    assert held['reason'] == 'recent_block_skipped' and calls.count(NAVER) == 1
    clock.sleep(DAY)
    service.run_web(settings, clock=clock, session=model, testing=True)
    assert calls.count(NAVER) == 2 and not model.calls
