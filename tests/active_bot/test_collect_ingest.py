"""Desktop Instagram ingestion boundary. No browser, session, or remote Instagram."""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hanset_active.collect.config import CollectSettings
from hanset_active.collect.service import run_ingest, run_status
from hanset_active.collect.state import State
from hanset_active.util.timeutil import FakeClock
from tests.active_bot.test_collect_deepseek import (
    HOURS, KEY, QWEN_KEY, Response, Session, _envelope, _hours_value, _qwen_key_file,
)

SENTINEL = 'UNIQUE_SOURCE_SENTINEL_do_not_store'


@pytest.fixture(autouse=True)
def clear_secrets(monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.delenv('TAVILY_API_KEY', raising=False)
    monkeypatch.delenv('HANSET_IG_SESSION', raising=False)


def _settings(tmp_path):
    settings = CollectSettings(data_dir=tmp_path / 'ig', active_dir=tmp_path / 'active', twin_dir=tmp_path / 'twin')
    settings.data_dir.mkdir()
    settings.inbox.mkdir()
    return settings


def _clock(hour=13, minute=30):
    # 13:30 KST is 04:30 UTC.
    return FakeClock(datetime(2026, 11, 7, hour - 9, minute, tzinfo=timezone.utc))


def _watch(settings):
    (settings.data_dir / 'watchlist.json').write_text(json.dumps({'schema_version': 1, 'venues': [{
        'enabled': True, 'venue_id': 'venue-1', 'venue_key': 'silver-seoul',
        'name': 'Silver Sauna', 'handle': 'silver.sauna'}]}))


def _line(text=None, **extra):
    body = {
        'venue_id': 'venue-1', 'venue_key': 'silver-seoul', 'account_handle': 'silver.sauna',
        'post_url': 'https://www.instagram.com/p/Abcd1234/',
        'operator_summary': text if text is not None else HOURS + '\n' + SENTINEL,
        'provenance': 'operator_summary',
        'seen_at': '2026-11-07T13:10:00+09:00',
    }
    body.update(extra)
    return json.dumps(body)


def _batch(settings, text=None, **extra):
    path = settings.inbox / 'batch.jsonl'
    path.write_text(_line(text, **extra) + '\n')
    return path


def _session():
    payload = {'items': [{'index': 0, 'claims': [{'field': 'hours', 'value': _hours_value(), 'quote': HOURS}]}]}
    return Session(_envelope(payload))


def _blob(root):
    return b'\n'.join(path.read_bytes() for path in root.rglob('*') if path.is_file() and path.name != 'batch.jsonl')


def test_outside_window_does_not_read_batch(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _watch(settings)
    path = _batch(settings)
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)

    def refuse(self, *args, **kwargs):
        raise AssertionError('batch read outside window')

    monkeypatch.setattr(Path, 'read_text', refuse)
    result = run_ingest(settings, clock=_clock(12, 59), session=_session())
    assert result['state'] == 'outside_window' and result['success'] is False and result['collected'] is False
    assert path.exists()
    assert SENTINEL.encode() not in _blob(settings.data_dir)
    later = run_ingest(settings, clock=_clock(14, 0), session=_session())
    assert later['state'] == 'outside_window'


def test_security_halt_keeps_no_raw_text_and_blocks_the_day(tmp_path, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    settings = _settings(tmp_path)
    _watch(settings)
    session = _session()
    path = _batch(settings, HOURS + ' verify you are human ' + SENTINEL)
    result = run_ingest(settings, clock=_clock(), session=session)
    assert result['state'] == 'halted' and result['reason'] == 'security_check'
    assert session.calls == [] and not path.exists()
    blob = _blob(settings.data_dir)
    assert SENTINEL.encode() not in blob and b'verify you are human' not in blob
    _batch(settings, HOURS)
    again = run_ingest(settings, clock=_clock(), session=session)
    assert again['state'] == 'halted_today' and session.calls == []
    assert (settings.inbox / 'batch.jsonl').exists()


def test_ingest_stores_source_claim_and_deletes_batch(tmp_path, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    settings = _settings(tmp_path)
    _watch(settings)
    path = _batch(settings)
    session = _session()
    result = run_ingest(settings, clock=_clock(), session=session)
    assert result['success'] is True and result['collected'] is True and result['reason'] == 'below_target'
    assert not path.exists() and session.calls
    import sqlite3
    db = sqlite3.connect(settings.data_dir / 'collector.sqlite3')
    db.row_factory = sqlite3.Row
    claim = db.execute('SELECT * FROM claims').fetchone()
    assert claim['source_kind'] == 'instagram' and claim['source_type'] == 'sns'
    assert claim['status'] == 'source_claim' and claim['rights'] == 'internal_review_only'
    assert claim['source_url'] == 'https://www.instagram.com/p/Abcd1234/'
    assert claim['quote'] == HOURS
    assert claim['evidence_origin'] == 'operator_summary'
    assert claim['evidence_role'] == 'summary_span'
    assert claim['verbatim_source'] == 0
    user = json.loads(session.calls[0]['json']['messages'][1]['content'])
    assert list(user[0]) == ['index', 'operator_summary']
    assert session.calls[0]['json']['messages'][0]['content'].startswith('The input is a minimal operator summary')
    blob = _blob(settings.data_dir)
    assert SENTINEL.encode() not in blob and KEY.encode() not in blob
    assert b'playwright' not in blob
    replay = settings.inbox / 'batch.jsonl'
    replay.write_text(_line() + '\n')
    second = run_ingest(settings, clock=_clock(), session=session)
    assert second['collected'] is False and len(session.calls) == 1
    status = run_status(settings)
    assert status['state'] == 'done'
    assert SENTINEL not in json.dumps(status) and KEY not in json.dumps(status)


def test_ingest_falls_back_to_qwen(tmp_path, monkeypatch):
    from hanset_active.collect.deepseek import QWEN_ENDPOINT
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    _qwen_key_file(tmp_path, monkeypatch)
    settings = _settings(tmp_path)
    _watch(settings)
    _batch(settings)
    primary = Session(Response(429, b'provider text'))
    qwen = _session()
    monkeypatch.setattr('hanset_active.collect.deepseek.new_qwen_session', lambda: qwen)
    result = run_ingest(settings, clock=_clock(), session=primary)
    assert result['success'] is True and result['collected'] is True
    assert result['provider'] == {'qwen': 1}
    assert len(primary.calls) == 1 and len(qwen.calls) == 1
    assert qwen.calls[0]['url'] == QWEN_ENDPOINT
    assert qwen.calls[0]['json']['messages'] == primary.calls[0]['json']['messages']
    assert qwen.calls[0]['json']['messages'][0]['content'].startswith('The input is a minimal operator summary')
    assert QWEN_KEY not in json.dumps(result)
    assert not (settings.inbox / 'batch.jsonl').exists()


def test_quota_photo_follow_and_flag(tmp_path, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    settings = _settings(tmp_path)
    _watch(settings)
    clock = _clock()
    state = State(settings.data_dir, clock)
    state.data['counts']['ig_reads'] = 20
    state.save()
    path = _batch(settings)
    session = _session()
    capped = run_ingest(settings, clock=clock, session=session)
    assert capped['reason'] == 'ig_quota' and path.exists() and session.calls == []
    assert SENTINEL.encode() not in _blob(settings.data_dir)
    state.data['counts']['ig_reads'] = 0
    state.save()
    path.write_text(_line(action='follow', text=HOURS + ' ' + SENTINEL) + '\n')
    refused = run_ingest(settings, clock=clock, session=session)
    assert refused['reason'] == 'action_not_automated' and session.calls == [] and not path.exists()
    path.write_text(_line(photo='data:image/jpeg;base64,PHOTOPAYLOAD' + SENTINEL) + '\n')
    photo = run_ingest(settings, clock=clock, session=session)
    assert photo['reason'] == 'photo_rejected' and b'PHOTOPAYLOAD' not in _blob(settings.data_dir)
    (settings.inbox / 'security_halt.json').write_text(json.dumps({
        'halt': True, 'reason': 'login_warning', 'text': SENTINEL}))
    path.write_text(_line() + '\n')
    flagged = run_ingest(settings, clock=clock, session=session)
    assert flagged['state'] == 'halted' and flagged['reason'] == 'login_warning'
    assert session.calls == [] and path.exists()
    assert not (settings.inbox / 'security_halt.json').exists()
    assert SENTINEL.encode() not in _blob(settings.data_dir)


def test_follow_allowed_watchlist_is_rejected(tmp_path):
    settings = _settings(tmp_path)
    (settings.data_dir / 'watchlist.json').write_text(json.dumps({'schema_version': 1, 'venues': [{
        'enabled': True, 'venue_id': 'venue-1', 'venue_key': 'silver-seoul', 'name': 'Silver Sauna',
        'handle': 'silver.sauna', 'follow_allowed': True}]}))
    _batch(settings)
    result = run_ingest(settings, clock=_clock(), session=_session())
    assert result['reason'] == 'watchlist_invalid'
    assert (settings.inbox / 'batch.jsonl').exists()


def test_cli_status_and_missing_targets(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    for name in ('DEEPSEEK_API_KEY', 'TAVILY_API_KEY', 'HANSET_DEEPSEEK_MODEL', 'HANSET_IG_SESSION'):
        env.pop(name, None)
    env['HANSET_IG_DIR'] = str(tmp_path / 'ig')
    env['HANSET_ACTIVE_DIR'] = str(tmp_path / 'active')
    env['HANSET_TWIN_DIR'] = str(tmp_path / 'twin')
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    status = subprocess.run([sys.executable, '-m', 'hanset_active', 'collect', 'status'], cwd=repo, env=env, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert status.returncode == 0
    assert json.loads(status.stdout)['state'] == 'idle'
    web = subprocess.run([sys.executable, '-m', 'hanset_active', 'collect', 'web'], cwd=repo, env=env, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert web.returncode == 2
    assert json.loads(web.stdout)['reason'] == 'no_targets'
    assert 'DEEPSEEK_API_KEY' not in web.stdout and 'DEEPSEEK_API_KEY' not in web.stderr


def test_package_has_no_browser_automation():
    root = Path(__file__).resolve().parents[2] / 'hanset_active' / 'collect'
    text = '\n'.join(path.read_text() for path in root.rglob('*.py'))
    for banned in ('playwright', 'storage_state', 'selenium', 'puppeteer', 'user_data_dir'):
        assert banned not in text
    script = (root.parents[1] / 'scripts' / 'run_collect_cron.sh').read_text()
    assert 'playwright' not in script and 'HANSET_IG_SESSION' in script
    assert 'DEEPSEEK_API_KEY' not in script
