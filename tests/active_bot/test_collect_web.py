"""Independent official-page collection. Instagram is not required."""
import json

import pytest

from hanset_active.collect.config import CollectSettings
from hanset_active.collect.service import run_web
from hanset_active.collect.state import State
from hanset_active.db import connect
from hanset_active.locks import RunLock
from hanset_active.search.tavily import SearchLead, SearchOutcome
from hanset_active.util.timeutil import FakeClock
from hanset_twin.daemon import ProcessLock
from tests.active_bot.test_collect_deepseek import (
    enable_qwen,     HOURS, KEY, QWEN_KEY, Response, Session, _envelope, _hours_value, _qwen_key_file,
)

SENTINEL = 'UNIQUE_SOURCE_SENTINEL_do_not_store'
PHONE = '0' + '10' + '-' + '1234' + '-' + '5678'
MAIL = 'fixture' + '@' + 'example.test'


@pytest.fixture(autouse=True)
def clear_secrets(monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.delenv('TAVILY_API_KEY', raising=False)
    monkeypatch.delenv('HANSET_DEEPSEEK_MODEL', raising=False)


def _settings(tmp_path):
    return CollectSettings(data_dir=tmp_path / 'ig', active_dir=tmp_path / 'active', twin_dir=tmp_path / 'twin')


def _page():
    filler = 'Official hotel sauna hours for guests. ' * 6
    return (
        '<html><head><title>Silver Sauna</title></head><body><main>'
        f'<p>{HOURS}</p><p>{filler}</p><p>{PHONE} {MAIL}</p><p>{SENTINEL}</p>'
        '</main></body></html>'
    )


def _targets(path, url, **extra):
    path.write_text(json.dumps({'schema_version': 1, 'targets': [{
        'enabled': True, 'venue_id': 'venue-1', 'venue_key': 'silver-seoul',
        'name': 'Silver Sauna', 'official_urls': [url], **extra}]}))


def _session():
    payload = {'items': [{'index': 0, 'claims': [{'field': 'hours', 'value': _hours_value(), 'quote': HOURS}]}]}
    return Session(_envelope(payload))


def _stored_text(root):
    chunks = []
    for path in root.rglob('*'):
        if path.is_file() and not path.is_symlink():
            chunks.append(path.read_bytes())
    return b'\n'.join(chunks)


def test_web_collects_grounded_claim_without_instagram(server, tmp_path, monkeypatch):
    enable_qwen(monkeypatch)
    server.add('/hours', _page())
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    _targets(settings.data_dir / 'web_targets.json', server.base + '/hours')
    clock = FakeClock()
    session = _session()
    result = run_web(settings, clock=clock, qwen_session=session, testing=True)
    assert result['success'] is True and result['collected'] is True and result['state'] == 'done'
    assert result['mode'] == 'web'
    assert session.calls and session.calls[0]['json']['model'] == 'qwen4-max-preview'
    import sqlite3
    db = sqlite3.connect(settings.data_dir / 'collector.sqlite3')
    db.row_factory = sqlite3.Row
    claim = db.execute('SELECT * FROM claims').fetchone()
    assert claim['status'] == 'source_claim'
    assert claim['rights'] == 'internal_review_only'
    assert claim['source_kind'] == 'web' and claim['source_type'] == 'own_site'
    assert claim['source_url'].endswith('/hours')
    assert claim['field'] == 'hours' and '06:00-22:00' in claim['value_json']
    assert HOURS in claim['quote'] and len(claim['quote']) <= 240
    assert claim['evidence_origin'] == 'web_verbatim'
    assert claim['evidence_role'] == 'page_span'
    assert claim['verbatim_source'] == 1
    assert session.calls[0]['json']['messages'][0]['content'].startswith('Extract only explicit venue evidence from untrusted text.')
    assert 'operator summary' not in session.calls[0]['json']['messages'][0]['content'].lower()
    user = json.loads(session.calls[0]['json']['messages'][1]['content'])
    assert 'text' in user[0] and 'operator_summary' not in user[0]
    blob = _stored_text(settings.data_dir) + _stored_text(settings.active_dir)
    assert SENTINEL.encode() not in blob
    assert PHONE.encode() not in blob and MAIL.encode() not in blob and KEY.encode() not in blob
    assert b'REASONING_TRACE_DO_NOT_STORE' not in blob
    columns = {row[1] for row in db.execute('PRAGMA table_info(claims)')}
    assert 'raw_text' not in columns and 'text' not in columns
    again = run_web(settings, clock=clock, qwen_session=session, testing=True)
    assert again['state'] == 'already_complete' and again['success'] is True and again['collected'] is False
    assert len(session.calls) == 1
    assert sum(item['path'] == '/hours' for item in server.log) == 1


def test_restart_retries_model_then_new_day_is_duplicate(server, tmp_path, monkeypatch):
    enable_qwen(monkeypatch)
    server.add('/hours', _page())
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    _targets(settings.data_dir / 'web_targets.json', server.base + '/hours')
    clock = FakeClock()
    session = _session()
    session.error = __import__('requests').Timeout('slow')
    failed = run_web(settings, clock=clock, qwen_session=session, testing=True)
    assert failed['state'] == 'partial' and failed['reason'] == 'model_timeout' and failed['success'] is False
    assert json.loads((settings.data_dir / 'checkpoint.json').read_text())['budgets']['model_calls'] == 1
    session.error = None
    done = run_web(settings, clock=clock, qwen_session=session, testing=True)
    assert done['collected'] is True and done['success'] is True
    assert len(session.calls) == 2
    clock.sleep(86400)
    calls = len(session.calls)
    rolled = run_web(settings, clock=clock, qwen_session=session, testing=True)
    assert rolled['collected'] is False and rolled['success'] is True
    assert any(page['disposition'] == 'unchanged' for page in rolled['pages'])
    assert len(session.calls) == calls
    from hanset_active.util.timeutil import KST
    saved = json.loads((settings.data_dir / 'checkpoint.json').read_text())
    assert saved['budgets']['model_calls'] == 0
    assert saved['day'] == clock.now().astimezone(KST).date().isoformat()


def test_robots_naver_and_locks(server, tmp_path, monkeypatch):
    monkeypatch.setattr('hanset_active.crawl_policy.korean_news', lambda url: True)
    enable_qwen(monkeypatch)
    server.add('/private', _page())
    server.add('/go', '', status=302, headers={'Location': server.base + '/hours', 'Content-Type': 'text/plain'})
    server.add('/hours', _page())
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    clock = FakeClock()
    session = _session()
    _targets(settings.data_dir / 'web_targets.json', server.base + '/private')
    refused = run_web(settings, clock=clock, qwen_session=session, testing=True)
    assert refused['success'] is True and refused['collected'] is False
    assert refused['pages'][0]['disposition'] == 'robots_disallowed'
    assert session.calls == []
    _targets(tmp_path / 'naver.json', 'https://naver.com/place')
    invalid = run_web(settings, targets_path=tmp_path / 'naver.json', clock=clock, qwen_session=session, testing=True)
    assert invalid['reason'] == 'targets_invalid' and session.calls == []
    _targets(tmp_path / 'named.json', server.base + '/hours', name='runport sauna')
    # name is a required field already set by _targets; overwrite through a raw file.
    (tmp_path / 'named.json').write_text(json.dumps({'schema_version': 1, 'targets': [{
        'enabled': True, 'venue_id': 'v', 'venue_key': 'runport-seoul', 'name': 'runport sauna',
        'official_urls': [server.base + '/hours']}]}))
    excluded = run_web(settings, targets_path=tmp_path / 'named.json', clock=clock, qwen_session=session, testing=True)
    assert excluded['reason'] == 'targets_invalid'
    state = State(settings.data_dir, clock)
    state.data['halt'] = {'day': state.data['day'], 'reason': 'security_check'}
    state.save()
    enable_qwen(monkeypatch)
    # The private URL is completed; point at a fresh page so web still runs while IG is halted.
    server.add('/fresh', _page())
    _targets(settings.data_dir / 'web_targets.json', server.base + '/fresh')
    resumed = run_web(settings, clock=clock, qwen_session=session, testing=True)
    assert resumed['collected'] is True
    assert json.loads((settings.data_dir / 'checkpoint.json').read_text())['halt']['reason'] == 'security_check'
    with ProcessLock(settings.data_dir / 'daemon.lock'):
        busy = run_web(settings, clock=clock, qwen_session=session, testing=True)
    assert busy['state'] == 'already_running'
    active = connect(settings.active_dir / 'active.sqlite3')
    _targets(settings.data_dir / 'web_targets.json', server.base + '/hours')
    # /hours was not completed on this halted checkpoint if fresh replaced it; hold the fetch lock.
    with RunLock(active, settings.active_dir, 'holder', clock=clock):
        deferred = run_web(settings, clock=clock, qwen_session=session, testing=True)
    assert deferred['reason'] == 'active_busy'
    active.close()


def test_tavily_lead_must_match_approved_domain(server, tmp_path, monkeypatch):
    enable_qwen(monkeypatch)
    server.add('/hours', _page())
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    target = {
        'schema_version': 1,
        'targets': [{
            'enabled': True, 'venue_id': 'venue-1', 'venue_key': 'silver-seoul', 'name': 'Silver Sauna',
            'official_urls': [], 'official_domains': ['127.0.0.1'],
            'tavily': {'venue_id': 'venue-1', 'field': 'hours'},
        }]}
    (settings.data_dir / 'web_targets.json').write_text(json.dumps(target))
    calls = []

    def search(db, venue_id, field, *, target='official_page', clock=None):
        calls.append((venue_id, field, target))
        return SearchOutcome('ok', 'ok', leads=(
            SearchLead('https://instagram.com/silver/', '', ''),
            SearchLead('https://naver.com/silver', '', ''),
            SearchLead(server.base + '/hours', '', ''),
        ))

    result = run_web(settings, clock=FakeClock(), qwen_session=_session(), search=search, testing=True)
    assert calls == [('venue-1', 'hours', 'official_page')]
    assert result['collected'] is True
    assert sum(item['path'] == '/hours' for item in server.log) == 1
    blob = _stored_text(settings.data_dir)
    assert b'instagram.com/silver' not in blob and b'naver.com/silver' not in blob


def test_missing_credentials_fetches_then_queues_without_model(server, tmp_path):
    server.add('/hours', _page())
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    _targets(settings.data_dir / 'web_targets.json', server.base + '/hours')
    session = _session()
    result = run_web(settings, clock=FakeClock(), qwen_session=session, testing=True)
    assert result['reason'] == 'missing_credentials' and result['success'] is False and result['collected'] is False
    assert session.calls == []
    assert any(item['path'] == '/hours' for item in server.log)
    assert result['counts']['pending'] == 1


def test_daily_duplicate_receipts_produce_distinct_transitions(server, tmp_path, monkeypatch):
    from hanset_active.collect.store import connect_readonly
    enable_qwen(monkeypatch)
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    _targets(settings.data_dir/'web_targets.json', server.base+'/hours')
    clock, session = FakeClock(), _session()
    for changed in (False, True, False, True):
        hour = '07:00-22:00' if changed else '06:00-22:00'
        server.add('/hours', _page().replace('06:00-22:00', hour))
        # Construct a response matching this day's grounded page.
        session = Session(_envelope({'items':[{'index':0,'claims':[{
            'field':'hours','value':{**_hours_value(),'hours':hour},'quote':HOURS.replace('06:00-22:00',hour)}]}]}))
        result = run_web(settings, clock=clock, qwen_session=session, testing=True)
        assert result['success']
        assert result['changes']['reason'] == 'ok'
        clock.sleep(86400)
    db = connect_readonly(settings.data_dir)
    assert db.execute('SELECT count(*) FROM web_change_events').fetchone()[0] == 3
    assert db.execute('SELECT count(*) FROM web_change_observations').fetchone()[0] == 4
    assert db.execute('SELECT count(*) FROM receipts').fetchone()[0] == 2
    db.close()


@pytest.mark.parametrize('kind', ['robots', 'blocked', 'empty', 'login'])
def test_unusable_pages_cannot_change_stored_baseline(server, tmp_path, monkeypatch, kind):
    monkeypatch.setattr('hanset_active.crawl_policy.korean_news', lambda url: True)
    from hanset_active.collect.store import connect_readonly
    enable_qwen(monkeypatch)
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    _targets(settings.data_dir/'web_targets.json', server.base+'/hours')
    server.add('/hours', _page())
    clock = FakeClock()
    assert run_web(settings, clock=clock, qwen_session=_session(), testing=True)['collected']
    clock.sleep(86400)
    if kind == 'robots':
        _targets(settings.data_dir/'web_targets.json', server.base+'/private')
        server.add('/private', _page())
    elif kind == 'blocked':
        server.add('/hours', '<main>verify you are human ' + 'challenge ' * 20 + '</main>')
    elif kind == 'empty':
        server.add('/hours', '<main></main>')
    else:
        server.add('/hours', '<main>Login</main>', status=401)
    run_web(settings, clock=clock, qwen_session=_session(), testing=True)
    db = connect_readonly(settings.data_dir)
    assert db.execute('SELECT count(*) FROM web_change_events').fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM web_change_observations').fetchone()[0] == 1
    assert db.execute('SELECT count(*) FROM web_change_baselines').fetchone()[0] == 1
    db.close()


def test_detector_failure_is_side_diagnostic_and_recovers_before_completed_return(server, tmp_path, monkeypatch):
    from hanset_active.collect import changes
    from hanset_active.collect.service import run_report
    from hanset_active.collect.store import connect_readonly
    enable_qwen(monkeypatch)
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    _targets(settings.data_dir/'web_targets.json', server.base+'/hours')
    clock = FakeClock()
    server.add('/hours', _page())
    assert run_web(settings, clock=clock, qwen_session=_session(), testing=True)['collected']
    clock.sleep(86400)
    server.add('/hours', _page().replace('06:00-22:00', '07:00-22:00'))
    session = Session(_envelope({'items':[{'index':0,'claims':[{
        'field':'hours','value':{**_hours_value(),'hours':'07:00-22:00'},
        'quote':HOURS.replace('06:00-22:00','07:00-22:00')}]}]}))
    def fail(*args):
        raise RuntimeError('injected')
    with monkeypatch.context() as patch:
        patch.setattr(changes, '_derive', fail)
        result = run_web(settings, clock=clock, qwen_session=session, testing=True)
    assert result['success'] and result['collected']
    assert result['changes']['reason'] == 'detector_error'
    assert run_report(settings)['changes']['pending_observations'] == 1
    fetched = len(server.log)
    result = run_web(settings, clock=clock, qwen_session=session, testing=True)
    assert result['state'] == 'already_complete' and result['changes']['reason'] == 'ok'
    assert len(server.log) == fetched
    db = connect_readonly(settings.data_dir)
    assert db.execute('SELECT count(*) FROM claims').fetchone()[0] == 2
    assert db.execute('SELECT count(*) FROM web_change_events').fetchone()[0] == 1
    db.close()


def test_default_web_fetches_derived_target_and_authorizes_observation(tmp_path, monkeypatch):
    from hanset_active.collect import plan
    from hanset_active.collect.targets import target_fingerprint
    from hanset_active.fetcher import FetchResult
    from tests.active_bot.test_collect_targets import eligible
    enable_qwen(monkeypatch)
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    active = connect(settings.active_dir / 'active.sqlite3')
    eligible(active, 1, website='https://venue1.example/hours')
    clock = FakeClock()
    expected = plan.assemble_targets(settings, now=clock.now(), testing=True)['targets'][0]
    fetched = []
    def fetch(self, url, *args, **kwargs):
        fetched.append(url)
        return FetchResult(url=url, outcome='ok', status=200, content=_page().encode(),
                           content_type='text/html', content_hash='fixture-hash')
    monkeypatch.setattr('hanset_active.collect.service.Fetcher.fetch', fetch)
    # _run_web must reuse the assembly for BOTH plan creation and local ordering.
    def no_snapshot(*args, **kwargs):
        raise AssertionError('execution reread the materialized snapshot')
    monkeypatch.setattr(plan, 'snapshot', no_snapshot)
    result = run_web(settings, clock=clock, qwen_session=_session(), testing=True)
    assert result['success'] and result['collected'] and fetched == expected['urls']
    import sqlite3
    with sqlite3.connect(settings.data_dir / 'collector.sqlite3') as db:
        obs = db.execute('SELECT venue_id,venue_key,target_fingerprint,processed FROM web_change_observations').fetchone()
        assert obs == (expected['venue_id'], expected['venue_key'], target_fingerprint(expected), 1)
        assert db.execute("SELECT count(*) FROM web_change_baselines WHERE state='active'").fetchone()[0] == 1
    assert result['changes']['reason'] == 'ok'
    assert not (settings.data_dir / 'web_targets.json').exists()
    active.close()


def test_same_day_instagram_completed_keys_do_not_abort_web(server, tmp_path, monkeypatch):
    # The daily checkpoint is shared: Instagram ingestion completes denied
    # instagram.com URLs, which must stay inert for the web run, not corrupt it.
    from hanset_active.collect.service import run_ingest
    from tests.active_bot.test_collect_ingest import _batch, _clock, _watch
    from tests.active_bot.test_collect_ingest import _session as ig_session
    enable_qwen(monkeypatch)
    monkeypatch.delenv('HANSET_IG_SESSION', raising=False)
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    settings.inbox.mkdir()
    _watch(settings)
    _batch(settings)
    clock = _clock()
    ingested = run_ingest(settings, clock=clock, qwen_session=ig_session())
    assert ingested['success'] is True and ingested['collected'] is True
    ig_url = 'https://www.instagram.com/p/Abcd1234/'
    assert ig_url in State(settings.data_dir, clock).data['completed']
    server.add('/hours', _page())
    _targets(settings.data_dir / 'web_targets.json', server.base + '/hours')
    result = run_web(settings, clock=clock, qwen_session=_session(), testing=True)
    assert result['reason'] != 'state_corrupt'
    assert result['success'] is True and result['collected'] is True
    assert sum(item['path'] == '/hours' for item in server.log) == 1
    # The foreign key is neither canonicalized away nor deleted from the checkpoint.
    completed = State(settings.data_dir, clock).data['completed']
    assert ig_url in completed and completed[ig_url] == 'applied'


def test_all_derived_targets_are_visited_in_one_run(tmp_path, monkeypatch):
    from hanset_active.fetcher import FetchResult
    from tests.active_bot.test_collect_targets import eligible
    enable_qwen(monkeypatch)
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    active = connect(settings.active_dir / 'active.sqlite3')
    for n in range(25):
        eligible(active, n, website=f'https://venue{n}.example/hours')
    fetched = []
    def blocked(self, url, **kwargs):
        fetched.append(url)
        return FetchResult(url=url, outcome='blocked', status=403)
    monkeypatch.setattr('hanset_active.collect.service.Fetcher.fetch', blocked)
    clock = FakeClock()
    first = run_web(settings, clock=clock, testing=True)
    assert first['success'] and len(fetched) == 25
    second = run_web(settings, clock=clock, testing=True)
    assert second['reason'] == 'recent_block_skipped'
    assert len(fetched) == second['counts']['pages'] == 25
    active.close()

class WebModel(Session):
    def __init__(self, respond):
        super().__init__()
        self.respond = respond
        self.items = []

    def post(self, url, **kwargs):
        items = json.loads(kwargs['json']['messages'][1]['content'])
        self.items.append(items)
        self.response = self.respond(items, len(self.items))
        return super().post(url, **kwargs)


def _fake_workset(tmp_path, monkeypatch, size, *, source=None, distinct=True):
    from hanset_active.fetcher import FetchResult

    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    targets = settings.data_dir / 'web_targets.json'
    urls = [f'https://spa-{n}.example.test/visit' for n in range(size)]
    if distinct:
        entries = [{'venue_id': f'spa-{n:04}', 'venue_key': f'spa-{n:04}', 'name': f'Seed Spa A {n}',
                    'official_urls': [url]} for n, url in enumerate(urls)]
    else:
        entries = [{'venue_id': 'seed-spa-a', 'venue_key': 'seed-spa-a', 'name': 'Seed Spa A',
                    'official_urls': urls}]
    targets.write_text(json.dumps({'schema_version': 1, 'targets': entries}))
    source = source or (lambda n: f'운영시간 06:00-{n + 10}:00. ' + 'Official venue information. ' * 6)
    pages = {url: source(n) for n, url in enumerate(urls)}
    fetched = []

    def fetch(_self, url, **kwargs):
        assert kwargs['url_allowed'](url)
        fetched.append(url)
        return FetchResult(url, 'ok', status=200, content=f'<main>{pages[url]}</main>'.encode(),
                           content_type='text/html')

    monkeypatch.setattr('hanset_active.collect.service.Fetcher.fetch', fetch)
    return settings, targets, urls, pages, fetched


def _hours_answer(items, *, used=20):
    import re

    mapped = []
    for item in reversed(items):
        match = re.search(r'운영시간 (06:00-\d\d:00)', item['text'])
        claims = ([{'field': 'hours', 'value': {'hours': match[1], 'scope': 'unspecified'}, 'quote': match[0]}]
                  if match else [])
        mapped.append({'index': item['index'], 'claims': claims})
    return _envelope({'items': mapped}, usage=used)


def test_qwen_only_one_six_item_call_maps_reordered_claims(tmp_path, monkeypatch):
    import sqlite3

    enable_qwen(monkeypatch)
    settings, path, urls, _, _ = _fake_workset(tmp_path, monkeypatch, 6)
    unavailable = Session()
    model = WebModel(lambda items, _: _hours_answer(items, used=3600))
    result = run_web(settings, targets_path=path, clock=FakeClock(),
                     session=unavailable, qwen_session=model, testing=True)
    assert result['success'] and result['counts']['committed'] == 6
    assert len(model.calls) == 1 and model.calls[0]['json']['max_tokens'] == 7200
    assert result['budgets']['output_tokens'] == 3600
    assert not unavailable.calls and not unavailable.probes
    with sqlite3.connect(settings.data_dir / 'collector.sqlite3') as db:
        actual = dict(db.execute("SELECT source_url,json_extract(value_json, '$.hours') FROM claims"))
    assert actual == {url: f'06:00-{n + 10}:00' for n, url in enumerate(urls)}


def test_malformed_batch_splits_once_and_restart_retries_only_failed_half(tmp_path, monkeypatch):
    import sqlite3

    enable_qwen(monkeypatch)
    settings, path, _, _, fetched = _fake_workset(tmp_path, monkeypatch, 6)
    clock = FakeClock()

    def respond(items, attempt):
        if attempt == 1:
            return _envelope({'items': [{'index': 0, 'claims': []}] * 6})
        if attempt == 3:
            return _envelope({'items': []})
        return _hours_answer(items)

    model = WebModel(respond)
    first = run_web(settings, targets_path=path, clock=clock, qwen_session=model, testing=True)
    assert [len(items) for items in model.items] == [6, 3, 3]
    assert first['state'] == 'partial' and first['collected']
    assert first['counts']['committed'] == first['counts']['pending'] == 3
    with sqlite3.connect(settings.active_dir / 'collect-runtime.sqlite3') as db:
        retry = {row[0] for row in db.execute('SELECT requested_url FROM extraction_retries')}
        assert len(retry) == 3
        assert db.execute('SELECT outcome FROM model_usage ORDER BY attempt_id').fetchall() == [
            ('model_invalid',), ('ok',), ('model_invalid',)]
    fetched.clear()
    second = run_web(settings, targets_path=path, clock=clock, qwen_session=model, testing=True)
    assert second['success'] and second['counts']['claims'] == 6
    assert set(fetched) == retry and len(model.items[-1]) == 3


@pytest.mark.parametrize('matching', [False, True])
def test_both_down_and_balance_failure_stops_deepseek(tmp_path, monkeypatch, capsys, matching):
    import sqlite3
    from tests.active_bot.test_collect_deepseek import healthy

    _qwen_key_file(tmp_path, monkeypatch)
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    clock = FakeClock()
    source = None if matching else lambda _: 'Official venue information. ' * 6
    settings, path, _, _, _ = _fake_workset(tmp_path, monkeypatch, 8, source=source)
    failing = Session(Response(429, b'PRIVATE_QWEN_BODY'))
    fallback = Session(Response(402, b'PRIVATE_DEEPSEEK_BODY'), probe=healthy())
    result = run_web(settings, targets_path=path, clock=clock, qwen_session=failing, session=fallback, testing=True)
    assert result['state'] == 'partial' and result['counts']['pending'] == (0 if matching else 8)
    assert result['counts']['rules_fallback'] == (8 if matching else 0)
    assert result['counts']['claims'] == (8 if matching else 0)
    assert result['deepseek_skipped'] == 'insufficient_balance'
    assert len(fallback.probes) == len(fallback.calls) == 1
    assert len(failing.calls) == 2 and result['provider'] == {'qwen': 2, 'deepseek': 1}
    with sqlite3.connect(settings.active_dir / 'collect-runtime.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM extraction_retries').fetchone()[0] == 8
    repaired = WebModel(lambda items, _: _hours_answer(items))
    recovered = run_web(settings, targets_path=path, clock=clock, qwen_session=repaired, session=fallback, testing=True)
    assert recovered['success'] and recovered['counts']['committed'] == 8
    assert recovered['counts']['rules_fallback'] == 0 and recovered['counts']['claims'] == result['counts']['claims']
    assert not recovered['collected'] and len(repaired.calls) == 2
    with sqlite3.connect(settings.data_dir / 'collector.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM claims').fetchone()[0] == result['counts']['claims']
        assert db.execute(
            "SELECT count(*) FROM claim_reconciliations WHERE disposition='model_confirmed'").fetchone()[0] == (
                8 if matching else 0)
    assert len(fallback.probes) == len(fallback.calls) == 1
    output = capsys.readouterr()
    blob = _stored_text(settings.data_dir) + _stored_text(settings.active_dir) + (output.out + output.err).encode()
    for sentinel in (KEY, QWEN_KEY, 'PRIVATE_QWEN_BODY', 'PRIVATE_DEEPSEEK_BODY', 'BALANCE_SENTINEL'):
        assert sentinel.encode() not in blob


def test_fragment_offsets_full_hash_and_failed_piece_are_page_atomic(tmp_path, monkeypatch):
    import sqlite3

    enable_qwen(monkeypatch)
    prefix = 'Official venue guide for guests. ' * 140
    quote = '운영시간 06:00-19:00'
    settings, path, urls, pages, _ = _fake_workset(tmp_path, monkeypatch, 1, source=lambda _: prefix + quote)
    clock = FakeClock()

    def respond(items, attempt):
        if attempt == 1:
            return _envelope({'items': []})
        if attempt == 3:
            return _envelope({'items': []})
        return _hours_answer(items)

    broken = WebModel(respond)
    first = run_web(settings, targets_path=path, clock=clock, qwen_session=broken, testing=True)
    assert [len(items) for items in broken.items] == [2, 1, 1]
    assert first['counts']['pending'] == 1 and not first['collected']
    with sqlite3.connect(settings.data_dir / 'collector.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM receipts').fetchone()[0] == 0

    model = WebModel(lambda items, _: _hours_answer(items))
    ready = run_web(settings, targets_path=path, clock=clock, qwen_session=model, testing=True)
    assert ready['collected'] and len(model.calls) == 1
    assert all(len(item['text']) <= 4000 for item in model.items[0])
    with sqlite3.connect(settings.data_dir / 'collector.sqlite3') as db:
        location = json.loads(db.execute('SELECT locator_json FROM claims').fetchone()[0])['char_offsets']
        assert location == [len(prefix), len(prefix + quote)]
    clock.sleep(86400)
    pages[urls[0]] = prefix.replace(' ', '   ') + '운영시간 ０６:００-１９:００'
    hit = run_web(settings, targets_path=path, clock=clock, qwen_session=model, testing=True)
    assert hit['counts']['unchanged'] == 1 and len(model.calls) == 1
    clock.sleep(86400)
    pages[urls[0]] = prefix + '운영시간 06:00-20:00'
    changed = run_web(settings, targets_path=path, clock=clock, qwen_session=model, testing=True)
    assert changed['collected'] and len(model.calls) == 2


def test_cache_recovers_between_receipt_and_index_write(tmp_path, monkeypatch):
    from hanset_active.collect import CollectError, service

    enable_qwen(monkeypatch)
    settings, path, _, _, _ = _fake_workset(tmp_path, monkeypatch, 1)
    model = WebModel(lambda items, _: _hours_answer(items))
    clock = FakeClock()
    with monkeypatch.context() as interrupted:
        def stop(*_args):
            raise CollectError('runtime_error')
        interrupted.setattr(service.WebExtraction, '_stored', stop)
        assert run_web(settings, targets_path=path, clock=clock, qwen_session=model, testing=True)['state'] == 'error'
    result = run_web(settings, targets_path=path, clock=clock, qwen_session=model, testing=True)
    assert result['counts']['unchanged'] == 1 and len(model.calls) == 1


def test_fragment_prefers_complete_sentences_and_does_not_split_decimals():
    from hanset_active.collect.web_extraction import fragments

    text = 'Venue note. ' + 'info ' * 797 + '12.5°C'
    assert text[3999:4001] == '.5'
    parts = list(fragments(text))
    assert [part['text'] for part in parts] == ['Venue note.', text[12:]]
    assert parts[1]['offset'] == 12


def test_unbounded_workset_drains_across_all_former_limits(tmp_path, monkeypatch):
    import sqlite3

    enable_qwen(monkeypatch)
    source = lambda _: 'Official guide for Seed Spa A. ' * 125
    settings, path, urls, _, fetched = _fake_workset(tmp_path, monkeypatch, 605, source=source)
    clock = FakeClock()
    model = WebModel(lambda items, _: _hours_answer(items, used=400))
    first = run_web(settings, targets_path=path, clock=clock, qwen_session=model, testing=True)
    assert first['success'] and set(fetched) == set(urls)
    assert first['counts']['pages'] == first['counts']['committed'] == 605
    assert first['budgets']['model_calls'] > 36
    assert first['budgets']['input_chars'] > 180000
    assert first['budgets']['output_tokens'] > 32000
    assert len(first['pages']) == 200
    assert max(len(items) for items in model.items) == 5
    with sqlite3.connect(settings.active_dir / 'collect-runtime.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM web_completions').fetchone()[0] == 605
    restored = run_web(settings, targets_path=path, clock=clock, qwen_session=model, testing=True)
    assert restored['reason'] == 'already_complete' and len(fetched) == 605


def test_sensitive_and_prohibited_evidence_dropped_without_losing_clean_fact(tmp_path, monkeypatch, capsys):
    import sqlite3

    enable_qwen(monkeypatch)
    private = 'Athlete: Fixture Person placed first'
    banned = 'MZ' + ' 사우나'
    bad_brand = '한' + '세트'
    datago_key = 'fixture-data-license-sentinel'
    key_path = tmp_path / 'fixture-datago'
    key_path.write_text(datago_key)
    key_path.chmod(0o600)
    monkeypatch.setenv('HANSET_DATAGO_KEY_FILE', str(key_path))
    sentinels = (private, banned, bad_brand, '디톡스', PHONE, MAIL, datago_key)
    quote = '운영시간 06:00-19:00'
    settings, path, _, _, _ = _fake_workset(tmp_path, monkeypatch, 1, source=lambda _: (
        '<p>Official information for Seed Spa A visitors.</p>' + f'<p>{quote}</p>' +
        ''.join(f'<p>{sentinel}. {quote}</p>' for sentinel in sentinels)))

    def respond(items, _attempt):
        assert all(sentinel not in items[0]['text'] for sentinel in (private, PHONE, MAIL, datago_key))
        return _envelope({'items': [{'index': 0, 'claims': [
            {'field': 'hours', 'value': {'hours': '06:00-19:00', 'scope': 'unspecified'}, 'quote': candidate}
            for candidate in [quote, *(f'{sentinel}. {quote}' for sentinel in sentinels)]
        ]}]})

    model = WebModel(respond)
    result = run_web(settings, targets_path=path, clock=FakeClock(), qwen_session=model, testing=True)
    assert result['success'] and result['counts']['claims'] == 1
    assert result['rejections'] == {'personal_data': 1, 'prohibited_wording': 3, 'sensitive_evidence': 3}
    with sqlite3.connect(settings.data_dir / 'collector.sqlite3') as db:
        assert db.execute('SELECT quote FROM claims').fetchall() == [(quote,)]
    output = capsys.readouterr()
    raw = _stored_text(settings.active_dir) + _stored_text(settings.data_dir) + (output.out + output.err).encode()
    assert all(sentinel.encode() not in raw for sentinel in sentinels)


def test_legacy_prefix_match_does_not_hide_long_page_suffix(tmp_path, monkeypatch):
    import sqlite3
    from hanset_active.collect.store import connect, digest, record

    enable_qwen(monkeypatch)
    prefix = 'aaaa ' * 800
    settings, path, urls, _, _ = _fake_workset(tmp_path, monkeypatch, 1,
                                                source=lambda _: prefix + '운영시간 06:00-20:00')
    clock = FakeClock()
    db = connect(settings.data_dir)
    record(db, kind='web', url=urls[0], text_hash=digest(prefix), disposition='applied', claims=[],
           venue_id='spa-0000', venue_key='spa-0000', seen_at=clock.now().isoformat(), clock=clock)
    db.close()
    model = WebModel(lambda items, _: _hours_answer(items))
    result = run_web(settings, targets_path=path, clock=clock, qwen_session=model, testing=True)
    assert result['success'] and result['counts']['claims'] == 1
    assert len(model.calls) == 1
    with sqlite3.connect(settings.data_dir / 'collector.sqlite3') as db:
        assert db.execute('SELECT hash_version FROM receipts ORDER BY rowid').fetchall() == [
            (None,), ('full-nfkc-1',)]


def test_legacy_checkpoint_with_more_than_100_web_completions_imports(tmp_path, monkeypatch):
    import sqlite3

    settings, path, urls, _, fetched = _fake_workset(tmp_path, monkeypatch, 110)
    clock = FakeClock()
    state = State(settings.data_dir, clock)
    ig_url = 'https://www.instagram.com/p/Fixture/'
    state.data['completed'] = {**dict.fromkeys(urls, 'applied'), ig_url: 'applied'}
    state.data['counts']['pages'] = 110
    state.save()
    result = run_web(settings, targets_path=path, clock=clock, testing=True)
    assert result['reason'] == 'already_complete' and not fetched
    assert State(settings.data_dir, clock).data['completed'] == {ig_url: 'applied'}
    with sqlite3.connect(settings.active_dir / 'collect-runtime.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM web_completions').fetchone()[0] == 110
