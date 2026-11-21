"""Operator-summary provenance. Fixtures are labeled samples, not captions."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hanset_active.collect.ground import ground
from hanset_active.collect.ingest import load_watchlist
from hanset_active.collect.service import run_ingest, run_report
from hanset_active.collect.store import connect, evidence_report, record
from hanset_active.extract import fields as field_rules
from tests.active_bot.test_collect_deepseek import Session, _envelope, enable_qwen
from tests.active_bot.test_collect_ingest import _blob, _clock, _settings

LABEL = 'SAMPLE operator summary, not an original caption.'
TAIL = 'NOTE_NOT_EVIDENCE'
CAPTION = 'CAPTION_SAMPLE_DO_NOT_STORE'
LEGACY = 'LEGACY_SAMPLE_NOT_A_CAPTION'
QUOTE_A = 'Weekday 09:00-17:00'
QUOTE_B = 'Weekday 12:00-20:00'
QUOTE_WEEKEND = 'Weekend 10:00-13:00'
POST_A = 'https://www.instagram.com/p/SamplePostA1/'
POST_B = 'https://www.instagram.com/reel/SampleReelB2/'
SEEN = '2026-11-07T13:10:00+09:00'
OLD_SCHEMA = '''
CREATE TABLE receipts (
    receipt_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('web', 'instagram')),
    source_url TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    disposition TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source_kind, source_url, content_hash)
);
CREATE TABLE claims (
    claim_id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    venue_key TEXT NOT NULL,
    field TEXT NOT NULL,
    value_json TEXT NOT NULL,
    quote TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status = 'source_claim'),
    rights TEXT NOT NULL CHECK (rights = 'internal_review_only'),
    rule_id TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    FOREIGN KEY (receipt_id) REFERENCES receipts(receipt_id)
);
'''


@pytest.fixture(autouse=True)
def clear_secrets(monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.delenv('TAVILY_API_KEY', raising=False)
    monkeypatch.delenv('HANSET_DEEPSEEK_MODEL', raising=False)


def _hours(quote):
    row = next(item for item in field_rules.extract(quote) if item['field'] == 'hours' and not item['ambiguous'])
    return {'hours': row['value']['hours'], 'scope': row['value']['scope']}


def _summary(quote):
    return LABEL + '\n' + quote + '\n' + TAIL


def _watch(settings, venues=None):
    if venues is None:
        venues = [{
            'enabled': True, 'venue_id': 'sample-venue', 'venue_key': 'sample-venue',
            'name': 'Sample Venue', 'handle': 'sample.venue',
        }]
    (settings.data_dir / 'watchlist.json').write_text(json.dumps({'schema_version': 1, 'venues': venues}))


def _line(summary, url=POST_A, **extra):
    body = {
        'venue_id': 'sample-venue', 'venue_key': 'sample-venue', 'account_handle': 'sample.venue',
        'post_url': url, 'operator_summary': summary, 'provenance': 'operator_summary', 'seen_at': SEEN,
    }
    body.update(extra)
    return json.dumps(body)


def _write(settings, lines):
    path = settings.inbox / 'batch.jsonl'
    path.write_text(''.join(line + '\n' for line in lines))
    return path


def _model(items):
    payload = {'items': items}
    return Session(_envelope(payload))


def _claim_item(index, quote):
    return {'index': index, 'claims': [{'field': 'hours', 'value': _hours(quote), 'quote': quote}]}


def _rows(settings):
    db = sqlite3.connect(settings.data_dir / 'collector.sqlite3')
    db.row_factory = sqlite3.Row
    rows = list(db.execute('SELECT * FROM claims ORDER BY quote'))
    db.close()
    return rows


def _no_choice(blob):
    if isinstance(blob, dict):
        assert not ({'current', 'selected', 'winner', 'preferred'} & set(blob))
        for value in blob.values():
            _no_choice(value)
    elif isinstance(blob, list):
        for value in blob:
            _no_choice(value)


def test_summary_is_not_an_original_quote_and_conflicts_stay(tmp_path, monkeypatch):
    enable_qwen(monkeypatch)
    settings = _settings(tmp_path)
    _watch(settings)
    lines = [
        _line(_summary(QUOTE_A), POST_A),
        _line(_summary(QUOTE_B), POST_B),
        _line(_summary(QUOTE_WEEKEND), POST_A.replace('SamplePostA1', 'SampleWeekendC3')),
    ]
    path = _write(settings, lines)
    session = _model([_claim_item(0, QUOTE_A), _claim_item(1, QUOTE_B), _claim_item(2, QUOTE_WEEKEND)])
    result = run_ingest(settings, clock=_clock(), qwen_session=session)
    assert result['success'] is True and result['collected'] is True
    assert not path.exists() and len(session.calls) == 1
    user = json.loads(session.calls[0]['json']['messages'][1]['content'])
    assert [set(item) for item in user] == [{'index', 'operator_summary'}] * 3
    assert 'text' not in user[0]
    assert session.calls[0]['json']['messages'][0]['content'].startswith('The input is a minimal operator summary')
    rows = _rows(settings)
    assert len(rows) == 3
    by_quote = {row['quote']: row for row in rows}
    assert set(by_quote) == {QUOTE_A, QUOTE_B, QUOTE_WEEKEND}
    for row in rows:
        assert row['evidence_origin'] == 'operator_summary'
        assert row['evidence_role'] == 'summary_span'
        assert row['verbatim_source'] == 0
        assert row['status'] == 'source_claim' and row['rights'] == 'internal_review_only'
    blob = _blob(settings.data_dir)
    assert TAIL.encode() not in blob and LABEL.encode() not in blob and CAPTION.encode() not in blob
    report = run_report(settings, clock=_clock())
    assert report['state'] == 'ok'
    assert {item['quote'] for item in report['claims']} == {QUOTE_A, QUOTE_B, QUOTE_WEEKEND}
    assert all(item['original_caption'] is False and item['verbatim_source'] == 0 for item in report['claims'])
    assert len(report['conflicts']) == 1
    conflict = report['conflicts'][0]
    assert conflict['field'] == 'hours' and conflict['scopes'] == ['weekday']
    assert {item['hours'] for item in conflict['values']} == {'09:00-17:00', '12:00-20:00'}
    assert len(conflict['claim_ids']) == 2
    _no_choice(report)
    weekend_id = by_quote[QUOTE_WEEKEND]['claim_id']
    assert weekend_id not in conflict['claim_ids']
    _write(settings, lines)
    replay = run_ingest(settings, clock=_clock(), qwen_session=session)
    assert replay['collected'] is False and len(session.calls) == 1
    assert [row['claim_id'] for row in _rows(settings)] == [row['claim_id'] for row in rows]
    assert [row['evidence_origin'] for row in _rows(settings)] == ['operator_summary'] * 3


def test_later_summary_does_not_overwrite_evidence(tmp_path, monkeypatch):
    enable_qwen(monkeypatch)
    settings = _settings(tmp_path)
    _watch(settings)
    first = _model([_claim_item(0, QUOTE_A)])
    _write(settings, [_line(_summary(QUOTE_A), POST_A)])
    assert run_ingest(settings, clock=_clock(), qwen_session=first)['collected'] is True
    second = _model([_claim_item(0, QUOTE_B)])
    _write(settings, [_line(_summary(QUOTE_B), POST_A)])
    assert run_ingest(settings, clock=_clock(), qwen_session=second)['collected'] is True
    rows = _rows(settings)
    assert {row['quote'] for row in rows} == {QUOTE_A, QUOTE_B}
    assert len({row['claim_id'] for row in rows}) == 2
    report = run_report(settings, clock=_clock())
    assert {item['hours'] for item in report['conflicts'][0]['values']} == {'09:00-17:00', '12:00-20:00'}
    _no_choice(report)


def test_text_field_and_bad_provenance_store_nothing(tmp_path, monkeypatch):
    enable_qwen(monkeypatch)
    settings = _settings(tmp_path)
    _watch(settings)
    session = _model([_claim_item(0, QUOTE_A)])
    caption = CAPTION + ' ' + QUOTE_A
    path = _write(settings, [_line(_summary(QUOTE_A), text=caption)])
    refused = run_ingest(settings, clock=_clock(), qwen_session=session)
    assert refused['reason'] == 'provenance_invalid' and refused['collected'] is False
    assert session.calls == [] and not path.exists()
    assert not (settings.data_dir / 'collector.sqlite3').exists()
    assert CAPTION.encode() not in _blob(settings.data_dir)
    path = _write(settings, [_line(_summary(QUOTE_A), provenance='web_verbatim')])
    wrong = run_ingest(settings, clock=_clock(), qwen_session=session)
    assert wrong['reason'] == 'provenance_invalid' and session.calls == [] and not path.exists()
    body = json.loads(_line(_summary(QUOTE_A)))
    del body['provenance']
    path = _write(settings, [json.dumps(body)])
    missing = run_ingest(settings, clock=_clock(), qwen_session=session)
    assert missing['reason'] == 'provenance_required' and missing['rejections']['provenance_required'] == 1
    assert session.calls == [] and not path.exists()
    path = _write(settings, [_line('x' * 481)])
    long = run_ingest(settings, clock=_clock(), qwen_session=session)
    assert long['reason'] == 'provenance_invalid' and session.calls == [] and not path.exists()
    path = _write(settings, [_line(_summary(QUOTE_A), note='extra')])
    extra = run_ingest(settings, clock=_clock(), qwen_session=session)
    assert extra['reason'] == 'input_invalid' and session.calls == [] and not path.exists()
    good = _line(_summary(QUOTE_A), POST_A)
    bad = _line(_summary(QUOTE_B), POST_B, text=caption)
    path = _write(settings, [good, bad])
    mixed = run_ingest(settings, clock=_clock(), qwen_session=session)
    assert mixed['collected'] is True and mixed['rejections']['provenance_invalid'] == 1
    assert not path.exists() and len(session.calls) == 1
    sent = json.loads(session.calls[0]['json']['messages'][1]['content'])
    assert len(sent) == 1 and caption not in sent[0]['operator_summary']
    rows = _rows(settings)
    assert [row['quote'] for row in rows] == [QUOTE_A]
    assert CAPTION.encode() not in _blob(settings.data_dir) and TAIL.encode() not in _blob(settings.data_dir)


def test_unresolved_and_ambiguous_handles_store_nothing(tmp_path, monkeypatch):
    enable_qwen(monkeypatch)
    settings = _settings(tmp_path)
    _watch(settings)
    session = _model([_claim_item(0, QUOTE_A)])
    path = _write(settings, [_line(_summary(QUOTE_A), venue_key='other-venue')])
    missed = run_ingest(settings, clock=_clock(), qwen_session=session)
    assert missed['reason'] == 'unresolved_venue' and session.calls == [] and not path.exists()
    assert LABEL.encode() not in _blob(settings.data_dir)
    _watch(settings, [
        {'enabled': True, 'venue_id': 'sample-venue', 'venue_key': 'sample-venue', 'name': 'Sample Venue', 'handle': 'sample.venue'},
        {'enabled': False, 'venue_id': 'closed-venue', 'venue_key': 'closed-venue', 'name': 'Closed Venue', 'handle': 'sample.venue'},
    ])
    assert len(load_watchlist(settings.data_dir / 'watchlist.json')) == 1
    _watch(settings, [
        {'enabled': True, 'venue_id': 'sample-venue', 'venue_key': 'sample-venue', 'name': 'Sample Venue', 'handle': 'Sample.Venue'},
        {'enabled': True, 'venue_id': 'other-venue', 'venue_key': 'other-venue', 'name': 'Other Venue', 'handle': 'sample.venue'},
    ])
    path = _write(settings, [_line(_summary(QUOTE_A))])
    ambiguous = run_ingest(settings, clock=_clock(), qwen_session=session)
    assert ambiguous['reason'] == 'watchlist_invalid' and path.exists() and session.calls == []
    assert not (settings.data_dir / 'collector.sqlite3').exists()


def test_legacy_rows_stay_unrecorded(tmp_path):
    settings = _settings(tmp_path)
    path = settings.data_dir / 'collector.sqlite3'
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA)
    raw.execute(
        'INSERT INTO receipts (receipt_id,source_kind,source_url,content_hash,disposition,created_at) VALUES (?,?,?,?,?,?)',
        ('receipt-legacy', 'web', 'https://example.invalid/hours', 'hash-legacy', 'applied', SEEN))
    raw.execute(
        '''INSERT INTO claims (
            claim_id, receipt_id, venue_id, venue_key, field, value_json, quote,
            source_url, source_kind, source_type, status, rights, rule_id, seen_at, content_hash
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        ('claim-legacy', 'receipt-legacy', 'legacy-sample', 'legacy-sample', 'hours',
         '{"hours":"09:00-17:00","scope":"unspecified"}', LEGACY, 'https://example.invalid/hours',
         'web', 'own_site', 'source_claim', 'internal_review_only', 'hours.explicit.v1', SEEN, 'hash-legacy'))
    raw.commit()
    before = raw.execute('SELECT quote, value_json, status, rights FROM claims').fetchone()
    raw.close()
    db = connect(settings.data_dir)
    migrated = db.execute('SELECT * FROM claims').fetchone()
    assert migrated['quote'] == before[0] == LEGACY
    assert migrated['value_json'] == before[1]
    assert migrated['evidence_origin'] is None
    assert migrated['evidence_role'] is None
    assert migrated['verbatim_source'] is None
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            '''INSERT INTO claims (
                claim_id, receipt_id, venue_id, venue_key, field, value_json, quote,
                source_url, source_kind, source_type, status, rights, rule_id, seen_at,
                content_hash, evidence_origin, evidence_role, verbatim_source
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            ('claim-new-null', 'receipt-legacy', 'legacy-sample', 'legacy-sample', 'hours',
             '{}', 'UNLABELED', 'https://example.invalid/hours', 'web', 'own_site',
             'source_claim', 'internal_review_only', 'hours.explicit.v1', SEEN, 'hash-other',
             None, None, None))
    db.rollback()
    record(
        db, kind='instagram', url=POST_A, text_hash='a' * 64, disposition='applied',
        claims=[{'field': 'hours', 'value': _hours(QUOTE_A), 'quote': QUOTE_A, 'rule_id': 'hours.explicit.v1'}],
        venue_id='sample-venue', venue_key='sample-venue', seen_at=SEEN, clock=_clock())
    legacy = db.execute('SELECT quote, evidence_origin, evidence_role, verbatim_source FROM claims WHERE claim_id=?', ('claim-legacy',)).fetchone()
    assert tuple(legacy) == (LEGACY, None, None, None)
    added = db.execute('SELECT evidence_origin, evidence_role, verbatim_source FROM claims WHERE quote=?', (QUOTE_A,)).fetchone()
    assert tuple(added) == ('operator_summary', 'summary_span', 0)
    db.close()
    again = connect(settings.data_dir)
    legacy = again.execute('SELECT quote, evidence_origin FROM claims WHERE claim_id=?', ('claim-legacy',)).fetchone()
    assert legacy['quote'] == LEGACY and legacy['evidence_origin'] is None
    report = evidence_report(again)
    found = next(item for item in report['claims'] if item['quote'] == LEGACY)
    assert found['evidence_origin'] is None and found['verbatim_source'] is None and found['original_caption'] is False
    again.close()
    source = Path(__file__).resolve().parents[2].joinpath('hanset_active', 'collect', 'store.py').read_text()
    assert 'UPDATE claims' not in source


def test_new_database_rejects_an_unlabeled_claim(tmp_path):
    settings = _settings(tmp_path)
    db = connect(settings.data_dir)
    db.execute(
        'INSERT INTO receipts (receipt_id,source_kind,source_url,content_hash,disposition,created_at) VALUES (?,?,?,?,?,?)',
        ('receipt-new', 'web', 'https://example.invalid/hours', 'hash-new', 'applied', SEEN))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            '''INSERT INTO claims (
                claim_id, receipt_id, venue_id, venue_key, field, value_json, quote,
                source_url, source_kind, source_type, status, rights, rule_id, seen_at,
                content_hash, evidence_origin, evidence_role, verbatim_source
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            ('claim-unlabeled', 'receipt-new', 'sample-venue', 'sample-venue', 'hours',
             '{}', 'UNLABELED_SAMPLE', 'https://example.invalid/hours', 'web', 'own_site',
             'source_claim', 'internal_review_only', 'hours.explicit.v1', SEEN, 'hash-new',
             None, None, None))
    db.rollback()
    assert db.execute('SELECT COUNT(*) FROM claims').fetchone()[0] == 0
    db.close()


def test_ground_rejects_a_caption_that_is_not_in_the_summary():
    summary = _summary(QUOTE_A)
    kept, dropped = ground(summary, [{'field': 'hours', 'value': _hours(QUOTE_A), 'quote': CAPTION + ' ' + QUOTE_A}])
    assert kept == [] and 'quote_not_in_text' in dropped
    kept, dropped = ground(summary, [{'field': 'hours', 'value': _hours(QUOTE_A), 'quote': QUOTE_A}])
    assert not dropped and kept[0]['quote'] == QUOTE_A and TAIL not in kept[0]['quote']


def test_report_command_reads_provenance_without_ingest(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    for name in ('DEEPSEEK_API_KEY', 'TAVILY_API_KEY', 'HANSET_DEEPSEEK_MODEL', 'HANSET_IG_SESSION'):
        env.pop(name, None)
    env['HANSET_IG_DIR'] = str(tmp_path / 'ig')
    env['HANSET_ACTIVE_DIR'] = str(tmp_path / 'active')
    env['HANSET_TWIN_DIR'] = str(tmp_path / 'twin')
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    report = subprocess.run(
        [sys.executable, '-m', 'hanset_active', 'collect', 'report'],
        cwd=repo, env=env, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert report.returncode == 0, report.stderr
    body = json.loads(report.stdout)
    assert body['state'] == 'ok' and body['claims'] == [] and body['conflicts'] == []
    assert 'DEEPSEEK_API_KEY' not in report.stdout
