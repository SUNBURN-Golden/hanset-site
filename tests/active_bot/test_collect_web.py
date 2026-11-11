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
    HOURS, KEY, QWEN_KEY, Response, Session, _envelope, _hours_value, _qwen_key_file,
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
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    server.add('/hours', _page())
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    _targets(settings.data_dir / 'web_targets.json', server.base + '/hours')
    clock = FakeClock()
    session = _session()
    result = run_web(settings, clock=clock, session=session, testing=True)
    assert result['success'] is True and result['collected'] is True and result['state'] == 'done'
    assert result['mode'] == 'web'
    assert session.calls and session.calls[0]['json']['model'] == 'deepseek-flash'
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
    again = run_web(settings, clock=clock, session=session, testing=True)
    assert again['state'] == 'already_complete' and again['success'] is True and again['collected'] is False
    assert len(session.calls) == 1
    assert sum(item['path'] == '/hours' for item in server.log) == 1


def test_restart_retries_model_then_new_day_is_duplicate(server, tmp_path, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    server.add('/hours', _page())
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    _targets(settings.data_dir / 'web_targets.json', server.base + '/hours')
    clock = FakeClock()
    session = _session()
    session.error = __import__('requests').Timeout('slow')
    failed = run_web(settings, clock=clock, session=session, testing=True)
    assert failed['state'] == 'partial' and failed['reason'] == 'model_timeout' and failed['success'] is False
    assert json.loads((settings.data_dir / 'checkpoint.json').read_text())['budgets']['model_calls'] == 1
    session.error = None
    done = run_web(settings, clock=clock, session=session, testing=True)
    assert done['collected'] is True and done['success'] is True
    assert len(session.calls) == 2
    clock.sleep(86400)
    calls = len(session.calls)
    rolled = run_web(settings, clock=clock, session=session, testing=True)
    assert rolled['collected'] is False and rolled['success'] is True
    assert any(page['disposition'] == 'duplicate' for page in rolled['pages'])
    assert len(session.calls) == calls
    from hanset_active.util.timeutil import KST
    saved = json.loads((settings.data_dir / 'checkpoint.json').read_text())
    assert saved['budgets']['model_calls'] == 0
    assert saved['day'] == clock.now().astimezone(KST).date().isoformat()


def test_extraction_falls_back_to_qwen(server, tmp_path, monkeypatch):
    import sqlite3
    from hanset_active.collect.deepseek import QWEN_ENDPOINT
    from hanset_active.collect.service import run_status
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    _qwen_key_file(tmp_path, monkeypatch)
    server.add('/hours', _page())
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    _targets(settings.data_dir / 'web_targets.json', server.base + '/hours')
    primary = Session(Response(429, b'provider text'))
    qwen = _session()
    monkeypatch.setattr('hanset_active.collect.deepseek.new_qwen_session', lambda: qwen)
    result = run_web(settings, clock=FakeClock(), session=primary, testing=True)
    assert result['state'] == 'done' and result['success'] is True and result['collected'] is True
    assert result['provider'] == {'qwen': 1}
    assert len(primary.calls) == 1 and len(qwen.calls) == 1
    assert qwen.calls[0]['url'] == QWEN_ENDPOINT
    status = json.loads((settings.data_dir / 'status.json').read_text())
    assert status['provider'] == {'qwen': 1}
    assert run_status(settings)['provider'] == {'qwen': 1}
    db = sqlite3.connect(settings.data_dir / 'collector.sqlite3')
    db.row_factory = sqlite3.Row
    claim = db.execute('SELECT * FROM claims').fetchone()
    assert claim['field'] == 'hours' and '06:00-22:00' in claim['value_json']
    assert claim['status'] == 'source_claim'
    db.close()
    assert QWEN_KEY.encode() not in _stored_text(settings.data_dir)


def test_extraction_both_fail_keeps_original_reason(server, tmp_path, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    _qwen_key_file(tmp_path, monkeypatch)
    server.add('/hours', _page())
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    _targets(settings.data_dir / 'web_targets.json', server.base + '/hours')
    primary = Session(Response(429, b'provider text'))
    qwen = Session(Response(402, b'credit exhausted'))
    monkeypatch.setattr('hanset_active.collect.deepseek.new_qwen_session', lambda: qwen)
    result = run_web(settings, clock=FakeClock(), session=primary, testing=True)
    assert result['state'] == 'partial' and result['reason'] == 'model_rate_limited'
    assert result['success'] is False and result['collected'] is False
    assert result['provider'] == {}
    assert len(primary.calls) == 1 and len(qwen.calls) == 1
    status = json.loads((settings.data_dir / 'status.json').read_text())
    assert status['provider'] == {}
    assert QWEN_KEY.encode() not in _stored_text(settings.data_dir)


def test_robots_naver_cap_and_locks(server, tmp_path, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    server.add('/private', _page())
    server.add('/go', '', status=302, headers={'Location': server.base + '/hours', 'Content-Type': 'text/plain'})
    server.add('/hours', _page())
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    clock = FakeClock()
    session = _session()
    _targets(settings.data_dir / 'web_targets.json', server.base + '/private')
    refused = run_web(settings, clock=clock, session=session, testing=True)
    assert refused['success'] is True and refused['collected'] is False
    assert refused['pages'][0]['disposition'] == 'robots_disallowed'
    assert session.calls == []
    _targets(tmp_path / 'naver.json', 'https://naver.com/place')
    invalid = run_web(settings, targets_path=tmp_path / 'naver.json', clock=clock, session=session, testing=True)
    assert invalid['reason'] == 'targets_invalid' and session.calls == []
    _targets(tmp_path / 'named.json', server.base + '/hours', name='runport sauna')
    # name is a required field already set by _targets; overwrite through a raw file.
    (tmp_path / 'named.json').write_text(json.dumps({'schema_version': 1, 'targets': [{
        'enabled': True, 'venue_id': 'v', 'venue_key': 'runport-seoul', 'name': 'runport sauna',
        'official_urls': [server.base + '/hours']}]}))
    excluded = run_web(settings, targets_path=tmp_path / 'named.json', clock=clock, session=session, testing=True)
    assert excluded['reason'] == 'targets_invalid'
    state = State(settings.data_dir, clock)
    state.data['counts']['pages'] = 20
    state.data['completed'] = {}
    state.save()
    _targets(settings.data_dir / 'web_targets.json', server.base + '/hours')
    before = sum(item['path'] == '/hours' for item in server.log)
    capped = run_web(settings, clock=clock, session=session, testing=True)
    assert capped['reason'] == 'daily_cap' and capped['success'] is False
    assert sum(item['path'] == '/hours' for item in server.log) == before
    state.data['counts']['pages'] = 0
    state.data['halt'] = {'day': state.data['day'], 'reason': 'security_check'}
    state.save()
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    # The private URL is completed; point at a fresh page so web still runs while IG is halted.
    server.add('/fresh', _page())
    _targets(settings.data_dir / 'web_targets.json', server.base + '/fresh')
    resumed = run_web(settings, clock=clock, session=session, testing=True)
    assert resumed['collected'] is True
    assert json.loads((settings.data_dir / 'checkpoint.json').read_text())['halt']['reason'] == 'security_check'
    with ProcessLock(settings.data_dir / 'daemon.lock'):
        busy = run_web(settings, clock=clock, session=session, testing=True)
    assert busy['state'] == 'already_running'
    active = connect(settings.active_dir / 'active.sqlite3')
    _targets(settings.data_dir / 'web_targets.json', server.base + '/hours')
    # /hours was not completed on this halted checkpoint if fresh replaced it; hold the fetch lock.
    with RunLock(active, settings.active_dir, 'holder', clock=clock):
        deferred = run_web(settings, clock=clock, session=session, testing=True)
    assert deferred['reason'] == 'active_busy'
    active.close()


def test_tavily_lead_must_match_approved_domain(server, tmp_path, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
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

    result = run_web(settings, clock=FakeClock(), session=_session(), search=search, testing=True)
    assert calls == [('venue-1', 'hours', 'official_page')]
    assert result['collected'] is True
    assert sum(item['path'] == '/hours' for item in server.log) == 1
    blob = _stored_text(settings.data_dir)
    assert b'instagram.com/silver' not in blob and b'naver.com/silver' not in blob


def test_missing_credentials_does_not_fetch(server, tmp_path):
    server.add('/hours', _page())
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    _targets(settings.data_dir / 'web_targets.json', server.base + '/hours')
    session = _session()
    result = run_web(settings, clock=FakeClock(), session=session, testing=True)
    assert result['reason'] == 'missing_credentials' and result['success'] is False and result['collected'] is False
    assert session.calls == []
    assert not any(item['path'] == '/hours' for item in server.log)


def test_daily_duplicate_receipts_produce_distinct_transitions(server, tmp_path, monkeypatch):
    from hanset_active.collect.store import connect_readonly
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
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
        result = run_web(settings, clock=clock, session=session, testing=True)
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
    from hanset_active.collect.store import connect_readonly
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    _targets(settings.data_dir/'web_targets.json', server.base+'/hours')
    server.add('/hours', _page())
    clock = FakeClock()
    assert run_web(settings, clock=clock, session=_session(), testing=True)['collected']
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
    run_web(settings, clock=clock, session=_session(), testing=True)
    db = connect_readonly(settings.data_dir)
    assert db.execute('SELECT count(*) FROM web_change_events').fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM web_change_observations').fetchone()[0] == 1
    assert db.execute('SELECT count(*) FROM web_change_baselines').fetchone()[0] == 1
    db.close()


def test_detector_failure_is_side_diagnostic_and_recovers_before_completed_return(server, tmp_path, monkeypatch):
    from hanset_active.collect import changes
    from hanset_active.collect.service import run_report
    from hanset_active.collect.store import connect_readonly
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    _targets(settings.data_dir/'web_targets.json', server.base+'/hours')
    clock = FakeClock()
    server.add('/hours', _page())
    assert run_web(settings, clock=clock, session=_session(), testing=True)['collected']
    clock.sleep(86400)
    server.add('/hours', _page().replace('06:00-22:00', '07:00-22:00'))
    session = Session(_envelope({'items':[{'index':0,'claims':[{
        'field':'hours','value':{**_hours_value(),'hours':'07:00-22:00'},
        'quote':HOURS.replace('06:00-22:00','07:00-22:00')}]}]}))
    def fail(*args):
        raise RuntimeError('injected')
    with monkeypatch.context() as patch:
        patch.setattr(changes, '_derive', fail)
        result = run_web(settings, clock=clock, session=session, testing=True)
    assert result['success'] and result['collected']
    assert result['changes']['reason'] == 'detector_error'
    assert run_report(settings)['changes']['pending_observations'] == 1
    fetched = len(server.log)
    result = run_web(settings, clock=clock, session=session, testing=True)
    assert result['state'] == 'already_complete' and result['changes']['reason'] == 'ok'
    assert len(server.log) == fetched
    db = connect_readonly(settings.data_dir)
    assert db.execute('SELECT count(*) FROM claims').fetchone()[0] == 2
    assert db.execute('SELECT count(*) FROM web_change_events').fetchone()[0] == 1
    db.close()
