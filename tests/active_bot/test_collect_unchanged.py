"""Offline soft deferral; fake fetchers, model sessions and collector clocks."""
import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest

from hanset_active.cli import main
from hanset_active.collect import service
from hanset_active.collect.changes import insert_observation
from hanset_active.collect.state import State
from hanset_active.collect.store import (
    connect, digest, recent_unchanged_web_targets, record,
    remember_web_target_block, remember_web_target_observation,
)
from hanset_active.collect.targets import canonical_requested_url
from hanset_active.fetcher import FetchResult
from hanset_active.util.timeutil import FakeClock, kst_now
from tests.active_bot.test_collect_deepseek import (
    enable_qwen,     HOURS, KEY, Session, _envelope, _hours_value,
)
from tests.active_bot.test_collect_plan import env, save_targets
from tests.active_bot.test_collect_subpages import ROOT, fake_pages, html, setup_run
from tests.active_bot.test_collect_targets import eligible, seed_rows

DAY = 86400
TEXT = (HOURS + ' Official guest information. ' * 8).strip()


@pytest.fixture(autouse=True)
def offline_credentials(monkeypatch):
    enable_qwen(monkeypatch)
    monkeypatch.delenv('HANSET_DEEPSEEK_MODEL', raising=False)


class Model(Session):
    """Return grounded or empty claims for every actual input document."""
    def __init__(self, *, empty=False, invalid=False):
        super().__init__()
        self.empty, self.invalid = empty, invalid

    def post(self, url, **kwargs):
        documents = json.loads(kwargs['json']['messages'][1]['content'])
        self.response = _envelope({} if self.invalid else {'items': [
            {'index': index, 'claims': [] if self.empty else [
                {'field': 'hours', 'value': _hours_value(), 'quote': HOURS},
            ]} for index in range(len(documents))
        ]})
        return super().post(url, **kwargs)


def seed_history(settings, url, clock, *, disposition='duplicate', text=TEXT):
    db = connect(settings.data_dir)
    try:
        if disposition == 'applied':
            record(db, kind='web', url=url, text_hash=digest(text), disposition='applied',
                   claims=[{'field': 'hours', 'value': _hours_value(), 'quote': HOURS,
                            'rule_id': 'fixture'}], venue_id='fixture', venue_key='fixture',
                   seen_at=clock.now().isoformat(), clock=clock)
        remember_web_target_observation(
            db, url, disposition, content_hash=digest(text),
            observed_at=clock.now().isoformat())
    finally:
        db.close()


def observations(settings):
    with sqlite3.connect(settings.data_dir / 'collector.sqlite3') as db:
        return db.execute('SELECT url_hash,content_hash,disposition,observed_at '
                          'FROM web_target_observations ORDER BY url_hash').fetchall()


@pytest.mark.parametrize('disposition', ['duplicate', 'applied', 'empty_extraction'])
@pytest.mark.parametrize('age', [2, 7, 8])
def test_recent_history_moves_behind_unseen_and_expires(tmp_path, monkeypatch, disposition, age):
    fresh = ROOT + '/unseen'
    settings, path = setup_run(tmp_path, monkeypatch, [ROOT, fresh])
    clock = FakeClock()
    seed_history(settings, ROOT, clock, disposition=disposition)
    clock.sleep(age * DAY)
    calls = fake_pages(monkeypatch, {ROOT: html(text=TEXT), fresh: html(text=TEXT)})
    result = service.run_web(settings, targets_path=path, clock=clock, qwen_session=Model(), testing=True)
    assert result['success'] and result['counts']['pages'] == 2
    assert calls == ([fresh, ROOT] if age < 7 else [ROOT, fresh])
    if age < 7:
        assert result['rejections'] == {'recent_unchanged_deferred': 1}
    else:
        assert 'recent_unchanged_deferred' not in result.get('rejections', {})


def test_empty_extraction_persists_and_deferred_fetch_uses_spare_capacity(tmp_path, monkeypatch):
    fresh = ROOT + '/unseen'
    settings, path = setup_run(tmp_path, monkeypatch, [ROOT])
    clock, model = FakeClock(), Model(empty=True)
    calls = fake_pages(monkeypatch, {ROOT: html(), fresh: html()})
    first = service.run_web(settings, targets_path=path, clock=clock, qwen_session=model, testing=True)
    assert {'url': ROOT, 'disposition': 'empty_extraction'} in first['pages']
    assert observations(settings)[0][2] == 'empty_extraction'
    clock.sleep(DAY)
    data = json.loads(path.read_text())
    data['targets'][0]['official_urls'] = [ROOT, fresh]
    path.write_text(json.dumps(data))
    second = service.run_web(settings, targets_path=path, clock=clock, qwen_session=model, testing=True)
    assert second['success'] and calls == [ROOT, fresh, ROOT]
    assert second['counts']['pages'] == 2 and len(model.calls) == 2
    assert second['rejections'] == {'recent_unchanged_deferred': 1}
    assert [page['disposition'] for page in second['pages'] if page['url']] == ['unchanged', 'empty_extraction']
    # The duplicate fast path also records a fresh scheduling timestamp.
    saved = {row[0]: row for row in observations(settings)}
    assert saved[digest(ROOT)][2:] == ('duplicate', kst_now(clock).isoformat())
    assert service.run_web(settings, targets_path=path, clock=clock, qwen_session=model,
                           testing=True)['reason'] == 'already_complete'


def test_discovered_child_records_history_without_change_authorization(tmp_path, monkeypatch):
    child = ROOT + '/price'
    settings, path = setup_run(tmp_path, monkeypatch)
    clock = FakeClock()
    calls = fake_pages(monkeypatch, {ROOT: html('<a href="/price">Price</a>'), child: html()})
    result = service.run_web(settings, targets_path=path, clock=clock, qwen_session=Model(empty=True), testing=True)
    assert result['success'] and calls == [ROOT, child]
    assert {row[0]: row[2] for row in observations(settings)} == {
        digest(ROOT): 'empty_extraction', digest(child): 'empty_extraction',
    }
    with sqlite3.connect(settings.data_dir / 'collector.sqlite3') as db:
        assert db.execute('SELECT requested_url FROM web_change_observations').fetchall() == [(ROOT,)]
    clock.sleep(DAY)
    again = service.run_web(settings, targets_path=path, clock=clock, qwen_session=Model(empty=True), testing=True)
    assert again['success'] and calls == [ROOT, child, ROOT, child]
    assert again['rejections'] == {'recent_unchanged_deferred': 2}
    assert {row[2] for row in observations(settings)} == {'duplicate'}


def test_deferred_child_yields_to_real_gap_derived_target(env, monkeypatch):
    settings, clock, active = env
    manual = eligible(active, 1, website=ROOT)
    manual.update(urls=[ROOT], domains={'venue.example'})
    other = 'https://other.example'
    eligible(active, 2, website=other)
    save_targets(settings.data_dir / 'web_targets.json', [manual])
    seed_history(settings, ROOT + '/price', clock, disposition='empty_extraction')
    clock.sleep(DAY)
    calls = fake_pages(monkeypatch, {ROOT: html('<a href="/price">Price</a>'), other: html(), ROOT + '/price': html()})
    result = service.run_web(settings, clock=clock, qwen_session=Model(empty=True), testing=True)
    assert result['success'] and calls == [ROOT, other, ROOT + '/price']
    assert result['rejections'] == {'recent_unchanged_deferred': 1}


def test_new_seed_yields_to_fresh_gap_but_runs_before_deferred_gap(env, monkeypatch):
    settings, clock, active = env
    recent, gap, new_seed = ('https://example.test/hours', 'https://example.test/pricing',
                             'https://spa.example.test/visit')
    established = eligible(active, 1, website=recent)
    eligible(active, 2, website=gap)
    seed_rows(active, name='Seed Spa A', website=new_seed, domain='example.test')
    seed_history(settings, recent, clock)
    clock.sleep(DAY)
    calls = fake_pages(monkeypatch, {url: html() for url in (recent, gap, new_seed)})
    first = service.run_web(settings, clock=clock, qwen_session=Model(empty=True), testing=True)
    assert first['success'] and calls == [gap, new_seed, recent]
    second = service.run_web(settings, clock=clock, qwen_session=Model(empty=True), testing=True)
    assert second['reason'] == 'already_complete' and second['counts']['pages'] == 3
    day = kst_now(clock).date().isoformat()
    plan = json.loads((settings.data_dir / 'plans' / (day + '.json')).read_text())
    assert plan['web_targets'][0]['venue_id'] == established['venue_id']
    assert first['rejections'] == {'recent_unchanged_deferred': 1}

    clock.sleep(DAY)
    later_seed = 'https://box.example.test/visit'
    seed_rows(active, name='Box B', website=later_seed, domain='example.test')
    calls = fake_pages(monkeypatch, {url: html() for url in (recent, gap, new_seed, later_seed)})
    later = service.run_web(settings, clock=clock, qwen_session=Model(empty=True), testing=True)
    assert later['success'] and calls == [later_seed, recent, gap, new_seed]
    assert later['rejections'] == {'recent_unchanged_deferred': 3}


def test_seed_revisits_rotate_oldest_first_after_unseen_work(env, monkeypatch):
    settings, clock, active = env
    urls = [f'https://example.test/venue/{index}' for index in range(3)]
    for index, url in enumerate(urls):
        seed_rows(active, name=f'Seed Spa A {index}', website=url, domain='example.test')
    assembled = service.assemble_targets(settings, now=kst_now(clock))
    plan_run = service.ensure_daily_plan(
        settings, State(settings.data_dir, clock), assembled['targets'], assembled['targets_status'],
        clock=clock, testing=True, ranking='local', assembled=assembled,
    )
    recent, oldest, unseen = [target['urls'][0] for target in plan_run['web_targets']]
    seed_history(settings, oldest, clock, disposition='error')
    clock.sleep(DAY)
    seed_history(settings, recent, clock, disposition='error')
    calls = fake_pages(monkeypatch, {url: html() for url in urls})
    result = service.run_web(settings, clock=clock, qwen_session=Model(empty=True), testing=True)
    assert result['success'] and calls == [unseen, oldest, recent]


def test_seed_duplicate_uses_gap_identity_once(env, monkeypatch):
    settings, clock, active = env
    url = 'https://example.test/visit'
    target = eligible(active, 1, website=url)
    seed_rows(active, name='Seed Spa A', website=url + '#official', domain='example.test')
    calls = fake_pages(monkeypatch, {url: html()})
    result = service.run_web(settings, clock=clock, qwen_session=Model(empty=True), testing=True)
    assert result['success'] and calls == [url]
    with sqlite3.connect(settings.data_dir / 'collector.sqlite3') as db:
        assert db.execute('SELECT venue_id FROM web_change_observations').fetchall() == [(target['venue_id'],)]


@pytest.mark.parametrize('destination,robots_redirect', [
    ('https://example.invalid/visit', False),
    ('http://example.test/visit', False),
    ('https://example.test/visit?sid=fixture', False),
    ('https://naver.me/venue', False),
    ('http://example.test/robots.txt', True),
])
def test_seed_redirect_rejected_before_destination_request(env, monkeypatch, destination, robots_redirect):
    if robots_redirect:
        monkeypatch.setattr('hanset_active.crawl_policy.korean_news', lambda url: True)
    settings, clock, active = env
    url, robots = 'https://example.test/entry', 'https://example.test/robots.txt'
    seed_rows(active, name='Seed Spa A', website=url, domain='example.test')
    calls = []

    def request(self, requested, kind, **kwargs):
        calls.append(requested)
        if requested == (robots if robots_redirect else url):
            return SimpleNamespace(status_code=302, headers={'Location': destination}), b'', '', None
        assert requested == robots
        return SimpleNamespace(status_code=200, headers={}), b'User-agent: *\nDisallow:', '', None

    monkeypatch.setattr(service.Fetcher, '_request', request)
    result = service.run_web(settings, clock=clock, qwen_session=Model(empty=True), testing=True)
    assert result['success'] and calls == ([robots] if robots_redirect else [url])
    assert result['pages'][-1]['disposition'] == ('robots_disallowed' if robots_redirect else 'denied')


@pytest.mark.parametrize('gate', ['credentials', 'daily_cap', 'model_budget', 'proxy', 'fetch'])
def test_deferred_only_runs_try_cache_and_honor_access_guards(tmp_path, monkeypatch, gate):
    settings, path = setup_run(tmp_path, monkeypatch)
    clock = FakeClock()
    seed_history(settings, ROOT, clock)
    clock.sleep(DAY)
    calls = fake_pages(monkeypatch, {ROOT: html(text=TEXT)})
    if gate == 'credentials':
        (settings.data_dir.parent / 'qwen-key').unlink()
        from hanset_active.collect.credentials import _read_key
        _read_key.cache_clear()
    elif gate in {'daily_cap', 'model_budget'}:
        state = State(settings.data_dir, clock)
        if gate == 'daily_cap':
            state.data['counts']['pages'] = 20
        else:
            state.data['budgets']['model_calls'] = 36
        state.save()
    elif gate == 'proxy':
        monkeypatch.setattr(service.web_proxy, 'configured', lambda: object())
        monkeypatch.setattr(service.web_proxy, 'preflight_with_retry',
                            lambda *args, **kwargs: ('proxy_unavailable', service.web_proxy.new_heal_status()))
    result = service.run_web(settings, targets_path=path, clock=clock, qwen_session=Model(), testing=True)
    assert result['reason'] == {
        'credentials': 'missing_credentials', 'daily_cap': 'ok',
        'model_budget': 'ok', 'proxy': 'ok', 'fetch': 'ok',
    }[gate]
    assert calls == [ROOT]
    if gate == 'proxy':
        assert result['web_egress'] == {'mode': 'direct', 'reason': 'proxy_unavailable', 'phase': 'preflight'}
    assert result['rejections'] == {'recent_unchanged_deferred': 1}


def test_count_coexists_with_blocks_and_survives_public_status(tmp_path, monkeypatch, capsys):
    fresh, blocked = ROOT + '/unseen', ROOT + '/held'
    settings, path = setup_run(tmp_path, monkeypatch, [ROOT, fresh, blocked])
    clock = FakeClock()
    for url in (ROOT, blocked):
        seed_history(settings, url, clock)
    db = connect(settings.data_dir)
    remember_web_target_block(db, blocked, 'blocked', status=403, clock=clock)
    db.close()
    clock.sleep(DAY)
    fake_pages(monkeypatch, {ROOT: html(text=TEXT), fresh: html(text=TEXT)})
    result = service.run_web(settings, targets_path=path, clock=clock, qwen_session=Model(), testing=True)
    expected = {'recent_block_skipped': 1, 'recent_unchanged_deferred': 1}
    assert result['rejections'] == expected
    assert service.public_result(result)['rejections'] == expected
    assert json.loads((settings.data_dir / 'status.json').read_text())['rejections'] == expected
    for name, directory in [('HANSET_IG_DIR', settings.data_dir), ('HANSET_ACTIVE_DIR', settings.active_dir),
                            ('HANSET_TWIN_DIR', settings.twin_dir)]:
        monkeypatch.setenv(name, str(directory))
    assert main(['collect', 'status']) == 0
    assert json.loads(capsys.readouterr().out)['rejections'] == expected
    assert blocked not in State(settings.data_dir, clock).data['completed']


def test_aliases_share_history_but_preserve_exact_requested_url(tmp_path, monkeypatch):
    alias, fresh = ROOT + '/hours/', ROOT + '/unseen'
    settings, path = setup_run(tmp_path, monkeypatch, [alias, alias + '#info', fresh])
    clock = FakeClock()
    seed_history(settings, alias + '?utm_source=nav', clock)
    clock.sleep(DAY)
    calls = fake_pages(monkeypatch, {alias: html(text=TEXT), fresh: html(text=TEXT)})
    result = service.run_web(settings, targets_path=path, clock=clock, qwen_session=Model(), testing=True)
    assert result['success'] and calls == [fresh, alias]
    assert result['rejections'] == {'recent_unchanged_deferred': 1}
    with sqlite3.connect(settings.data_dir / 'collector.sqlite3') as db:
        assert db.execute('SELECT source_url FROM receipts WHERE source_url=?', (alias,)).fetchone()
    assert len(observations(settings)) == 2


@pytest.mark.parametrize('matching', [True, False])
def test_old_receipt_uses_only_recent_matching_legacy_observation(tmp_path, matching):
    db, clock = connect(tmp_path / 'collector'), FakeClock()
    url, text_hash = ROOT + '/hours/', digest(TEXT)
    record(db, kind='web', url=url, text_hash=text_hash, disposition='applied',
           claims=[{'field': 'hours', 'value': _hours_value(), 'quote': HOURS, 'rule_id': 'fixture'}],
           venue_id='fixture', venue_key='fixture', seen_at=clock.now().isoformat(), clock=clock)
    evidence = [tuple(row) for row in db.execute('SELECT * FROM receipts')]
    clock.sleep((20 if matching else 4) * DAY)
    canonical = canonical_requested_url(url)
    row = {'url': canonical, 'final_url': canonical, 'venue_id': 'fixture', 'venue_key': 'fixture',
           'seen_at': clock.now().isoformat(), 'content_hash': text_hash if matching else digest('changed'),
           'source_authorization': {'requested_url': canonical, 'source_identity': canonical,
                                    'target_fingerprint': digest('fixture')}}
    insert_observation(db, row, [])
    clock.sleep(2 * DAY)
    assert recent_unchanged_web_targets(db, clock=clock) == ({digest(canonical)} if matching else set())
    assert [tuple(row) for row in db.execute('SELECT * FROM receipts')] == evidence
    clock.sleep(5 * DAY)
    assert recent_unchanged_web_targets(db, clock=clock) == set()
    db.close()


def test_legacy_receipt_without_observation_uses_creation_time(tmp_path):
    db, clock = connect(tmp_path / 'collector'), FakeClock()
    record(db, kind='web', url=ROOT, text_hash=digest(TEXT), disposition='applied',
           claims=[], venue_id='fixture', venue_key='fixture', seen_at=clock.now().isoformat(), clock=clock)
    clock.sleep(DAY)
    assert recent_unchanged_web_targets(db, clock=clock) == {digest(ROOT)}
    clock.sleep(6 * DAY)
    assert not recent_unchanged_web_targets(db, clock=clock)
    db.close()


def test_failed_extraction_supersedes_old_applied_before_next_day_retry(tmp_path, monkeypatch):
    fresh = ROOT + '/unseen'
    settings, path = setup_run(tmp_path, monkeypatch)
    clock = FakeClock()
    seed_history(settings, ROOT, clock, disposition='applied')
    clock.sleep(DAY)
    calls = fake_pages(monkeypatch, {ROOT: html(text=TEXT + ' Changed page.'), fresh: html(text=TEXT)})
    failed = service.run_web(settings, targets_path=path, clock=clock, qwen_session=Model(invalid=True), testing=True)
    assert failed['reason'] == 'model_invalid'
    assert observations(settings)[0][2] == 'pending_extraction'
    assert not State(settings.data_dir, clock).data['completed']
    clock.sleep(DAY)
    data = json.loads(path.read_text())
    data['targets'][0]['official_urls'].append(fresh)
    path.write_text(json.dumps(data))
    retried = service.run_web(settings, targets_path=path, clock=clock, qwen_session=Model(), testing=True)
    assert retried['success'] and calls == [ROOT, ROOT, fresh]
    assert 'recent_unchanged_deferred' not in retried.get('rejections', {})
    assert observations(settings)[0][2] == 'applied'


@pytest.mark.parametrize('outcome', ['pending_extraction', 'empty_body', 'error', 'blocked'])
def test_new_nonqualifying_outcome_controls_over_legacy_applied(tmp_path, outcome):
    db, clock = connect(tmp_path / 'collector'), FakeClock()
    record(db, kind='web', url=ROOT, text_hash=digest(TEXT), disposition='applied',
           claims=[{'field': 'hours', 'value': _hours_value(), 'quote': HOURS, 'rule_id': 'fixture'}],
           venue_id='fixture', venue_key='fixture', seen_at=clock.now().isoformat(), clock=clock)
    clock.sleep(DAY)
    remember_web_target_observation(db, ROOT, outcome, observed_at=clock.now().isoformat())
    assert not recent_unchanged_web_targets(db, clock=clock)
    db.close()


def test_applied_history_requires_matching_applied_receipt(tmp_path, monkeypatch):
    settings, _ = setup_run(tmp_path, monkeypatch)
    clock = FakeClock()
    seed_history(settings, ROOT, clock, disposition='applied')
    db = connect(settings.data_dir)
    remember_web_target_observation(db, ROOT, 'applied', content_hash=digest('different'),
                                    observed_at=clock.now().isoformat())
    assert not recent_unchanged_web_targets(db, clock=clock)
    db.close()


def test_upgrade_leaves_evidence_unchanged_and_excludes_unsafe_and_instagram_history(tmp_path, monkeypatch):
    settings, _ = setup_run(tmp_path, monkeypatch)
    db, clock = connect(settings.data_dir), FakeClock()
    db.execute('''INSERT INTO receipts (receipt_id,source_kind,source_url,content_hash,disposition,created_at)
                  VALUES (?,?,?,?,?,?)''', ('legacy', 'web', ROOT + '/hours?sid=fixture', digest(TEXT),
                                            'empty_extraction', clock.now().isoformat()))
    db.commit()
    record(db, kind='instagram', url='https://www.instagram.com/p/Fixture/', text_hash=digest(TEXT),
           disposition='applied', claims=[], venue_id='fixture', venue_key='fixture',
           seen_at=clock.now().isoformat(), clock=clock)
    before = [tuple(row) for row in db.execute('SELECT * FROM receipts')]
    db.execute('DROP TABLE web_target_observations')
    db.commit()
    db.close()
    db = connect(settings.data_dir)
    assert [tuple(row) for row in db.execute('SELECT * FROM receipts')] == before
    assert not recent_unchanged_web_targets(db, clock=clock)
    db.close()
    rows = [{'url': 'https://www.instagram.com/p/AnotherFixture/', 'text': TEXT,
             'venue_id': 'fixture', 'venue_key': 'fixture', 'seen_at': clock.now().isoformat()}]
    state, session = State(settings.data_dir, clock), Model(empty=True)
    for _ in range(2):  # Both new and duplicate Instagram paths stay untouched.
        service._extract_summaries(rows, settings=settings, state=state, session=None, qwen_session=session, clock=clock)
    assert not observations(settings)


def test_cache_lookup_preserves_observed_fetch_time(tmp_path, monkeypatch):
    settings, path = setup_run(tmp_path, monkeypatch)
    clock = FakeClock()
    seed_history(settings, ROOT, clock, disposition='applied')
    clock.sleep(DAY)
    fake_pages(monkeypatch, {ROOT: html(text=TEXT)})
    seen_at = kst_now(clock).isoformat()
    model = Model()
    result = service.run_web(settings, targets_path=path, clock=clock, qwen_session=model, testing=True)
    assert result['counts']['unchanged'] == 1 and not model.calls
    assert observations(settings)[0][3] == seen_at


def test_future_timestamp_does_not_defer_and_latest_observation_survives_expiry(tmp_path):
    db, clock = connect(tmp_path / 'collector'), FakeClock()
    future = clock.now() + timedelta(days=1)
    remember_web_target_observation(db, ROOT, 'duplicate', content_hash=digest(TEXT),
                                    observed_at=future.isoformat())
    assert not recent_unchanged_web_targets(db, clock=clock)
    clock.sleep(DAY)
    assert recent_unchanged_web_targets(db, clock=clock) == {digest(ROOT)}
    clock.sleep(7 * DAY)
    assert not recent_unchanged_web_targets(db, clock=clock)
    assert db.execute('SELECT observed_at FROM web_target_observations').fetchone()[0] == future.isoformat()
    db.close()


@pytest.mark.parametrize('proxy_failed', [False, True])
def test_failed_fetch_replaces_previous_qualifying_outcome(tmp_path, monkeypatch, proxy_failed):
    settings, path = setup_run(tmp_path, monkeypatch)
    clock = FakeClock()
    seed_history(settings, ROOT, clock)
    clock.sleep(DAY)

    def fetch(self, url, **kwargs):
        return FetchResult(url, 'error', reason='proxy_unavailable' if proxy_failed else 'ConnectionError')

    monkeypatch.setattr(service.Fetcher, 'fetch', fetch)
    result = service.run_web(settings, targets_path=path, clock=clock, testing=True)
    assert result['rejections'] == {'recent_unchanged_deferred': 1}
    assert result['counts']['pages'] == 1
    assert observations(settings)[0][2:] == ('error', kst_now(clock).isoformat())
    clock.sleep(DAY)
    db = connect(settings.data_dir)
    assert not recent_unchanged_web_targets(db, clock=clock)
    db.close()
