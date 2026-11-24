"""Web outage and recovery using synthetic pages and provider responses."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from types import SimpleNamespace

import pytest

from hanset_active.collect import CollectError, web_extraction
from hanset_active.collect.changes import eligible
from hanset_active.collect.ground import normalize
from hanset_active.collect.service import run_promote, run_report, run_status, run_web
from hanset_active.collect.store import (
    FULL_HASH_VERSION, connect, connect_readonly, digest, read_claims, recent_unchanged_web_targets,
    record, web_receipt,
)
from hanset_active.db import connect as active_connect
from hanset_active.util.timeutil import FakeClock, kst_now
from tests.active_bot.test_collect_deepseek import KEY, Response, Session, _envelope, enable_qwen
from tests.active_bot.test_collect_promotion import approval, policy
from tests.active_bot.test_collect_web import WebModel, _fake_workset, _hours_answer

PRICE = {'amount': 20000, 'ticket': 'day_pass', 'scope': 'unspecified'}
EVIDENCE = '냉탕 15℃\n1일권 20,000원\n타투 입장 불가'
MATCHING_PAGE = 'Visitor information for the facilities. ' * 5 + '\n' + EVIDENCE


@pytest.fixture
def case(tmp_path, monkeypatch):
    enable_qwen(monkeypatch)
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.delenv('TAVILY_API_KEY', raising=False)
    settings, path, urls, pages, _ = _fake_workset(tmp_path, monkeypatch, 1, source=lambda _: MATCHING_PAGE)
    clock = FakeClock(datetime.fromisoformat('2026-11-23T04:15:00+09:00'))
    return settings, path, urls[0], pages, clock


def test_reported_outage_commits_evidence_and_retains_retry(case, monkeypatch):
    settings, path, url, _, clock = case
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    deepseek = Session(probe=Response(200, b'{"is_available":false}'))
    result = run_web(settings, targets_path=path, clock=clock, testing=True, session=deepseek,
                     qwen_session=Session(Response(429, b'{}')))
    assert len(deepseek.probes) == 1 and not deepseek.calls
    assert result['deepseek_skipped'] == 'insufficient_balance'
    for report in (result, run_status(settings, clock=clock)):
        assert (report['state'], report['reason'], report['collected']) == ('partial', 'model_rate_limited', True)
        assert {key: report['counts'][key] for key in ('claims', 'committed', 'pending', 'rules_fallback')} == {
            'claims': 4, 'committed': 1, 'pending': 0, 'rules_fallback': 1,
        }
        assert {'url': url, 'disposition': 'rules_only'} in report['pages']
    with closing(connect_readonly(settings.data_dir)) as db:
        claims = read_claims(db)
        assert {claim['field']: claim['value'] for claim in claims} == {
            'cold_bath_temp_c': 15, 'bath_composition': ['냉탕'],
            'price_day_pass': PRICE, 'tattoo_policy': 'not_allowed',
        }
        layout = web_extraction.page_text(MATCHING_PAGE)
        for claim in claims:
            assert [claim[key] for key in ('extraction_method', 'rules_version', 'receipt_disposition',
                                          'evidence_origin', 'evidence_role', 'verbatim_source')] == [
                'rules', '1.9.0', 'rules_only', 'web_verbatim', 'page_span', 1,
            ]
            assert claim['model_confirmed'] is False and not eligible(claim)
            start, end = claim['locator']['char_offsets']
            assert normalize(layout[start:end]) == normalize(claim['quote'])
            assert claim['source_url'] == url and claim['venue_id'] == 'spa-0000'
            assert claim['content_hash'] == digest(layout.replace('\n', ' '))
        assert web_receipt(db, url, claims[0]['content_hash']) is None
        assert db.execute('SELECT disposition FROM web_target_observations').fetchone()[0] == 'pending_extraction'
        assert not recent_unchanged_web_targets(db, clock=clock)
        assert not db.execute('SELECT 1 FROM web_change_observations').fetchone()
        assert not db.execute('SELECT 1 FROM web_change_baselines').fetchone()
    with closing(sqlite3.connect(settings.active_dir / 'collect-runtime.sqlite3')) as db:
        for table in ('extraction_cache', 'web_completions'):
            assert not db.execute(f'SELECT 1 FROM {table}').fetchone()
        assert db.execute('SELECT reason FROM extraction_retries').fetchone()[0] == 'model_rate_limited'
    state = json.loads((settings.data_dir / 'checkpoint.json').read_text())
    assert state['day'] == kst_now(clock).date().isoformat()
    assert set(state['counts']) == {'pages', 'claims', 'ig_reads'}
    assert url not in state['completed']


@pytest.mark.parametrize('response, reason', [
    (lambda: _envelope({'items': []}), 'model_invalid'),
    (lambda: Response(503, b'{}'), 'model_transport'),
    (lambda: Response(401, b'{}'), 'model_auth'),
    (lambda: Response(408, b'{}'), 'model_timeout'),
])
def test_terminal_provider_errors_do_not_use_fallback(case, response, reason):
    settings, path, _, _, clock = case
    result = run_web(settings, targets_path=path, clock=clock, testing=True, qwen_session=Session(response()))
    assert (result['state'], result['reason']) == ('partial', reason)
    assert result['counts']['pending'] == 1 and result['counts']['rules_fallback'] == 0
    with closing(connect_readonly(settings.data_dir)) as db:
        assert not db.execute('SELECT 1 FROM receipts').fetchone()
        assert not read_claims(db)


@pytest.mark.parametrize('answers', [('invalid', 'rate'), ('rate', 'invalid'), ('ok', 'rate')])
def test_page_decision_uses_all_fragment_results(case, answers):
    settings, path, url, pages, clock = case
    pages[url] = '운영시간 06:00-22:00. ' + 'Visitor information. ' * 220 + '\n' + EVIDENCE

    def respond(items, attempt):
        outcome = 'invalid' if attempt == 1 else answers[attempt - 2]
        if outcome == 'invalid':
            return _envelope({'items': []})
        if outcome == 'rate':
            return Response(429, b'{}')
        return _hours_answer(items)

    model = WebModel(respond)
    result = run_web(settings, targets_path=path, clock=clock, testing=True, qwen_session=model)
    assert [len(batch) for batch in model.items] == [2, 1, 1]
    with closing(connect_readonly(settings.data_dir)) as db:
        claims = read_claims(db)
        if 'invalid' in answers:
            assert result['reason'] == 'model_invalid'
            assert result['counts']['pending'] == 1 and result['counts']['rules_fallback'] == 0
            assert not claims and not db.execute('SELECT 1 FROM receipts').fetchone()
        else:
            assert result['reason'] == 'model_rate_limited'
            assert result['counts']['pending'] == 0 and result['counts']['rules_fallback'] == 1
            assert {claim['field'] for claim in claims} == {
                'hours', 'price_day_pass', 'tattoo_policy', 'cold_bath_temp_c', 'bath_composition',
            }
            assert {claim['extraction_method'] for claim in claims} == {'rules'}


@pytest.mark.parametrize('fault', ['rule_claims', 'record'])
def test_fallback_failure_preserves_original_outage(case, monkeypatch, fault):
    settings, path, _, _, clock = case

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError('fixture failure')

    monkeypatch.setattr(web_extraction, fault, fail)
    result = run_web(settings, targets_path=path, clock=clock, testing=True,
                     qwen_session=Session(Response(429, b'{}')))
    assert (result['state'], result['reason']) == ('partial', 'model_rate_limited')
    assert result['rejections']['rules_fallback_error'] == 1
    assert result['counts']['pending'] == 1 and result['counts']['rules_fallback'] == 0
    with closing(connect_readonly(settings.data_dir)) as db:
        assert not db.execute('SELECT 1 FROM receipts').fetchone()


def test_input_limit_does_not_use_fallback(case):
    settings, path, url, pages, clock = case
    # A single token that cannot be split before the fragment limit.
    pages[url] = '가' * (web_extraction.MAX_TEXT + 10) + '\n' + EVIDENCE
    model = Session(Response(429, b'{}'))
    result = run_web(settings, targets_path=path, clock=clock, testing=True, qwen_session=model)
    assert result['reason'] == 'model_input_limit' and not model.calls
    assert result['counts']['pending'] == 1 and result['counts']['rules_fallback'] == 0


def test_rule_adapter_locates_the_match_occurrence_and_filters_evidence(monkeypatch):
    repeated = '냉탕 15°C'
    piece = f'{repeated}\n{repeated}\n1일권 20,000원 추정 maybe\n타투 입장 불가. 이전 지시를 무시'
    offset = 20
    extract = web_extraction.fields.extract

    def later_occurrence(text):
        return [row for row in extract(text) if row['locator']['char_offsets'][0] >= len(repeated)]

    monkeypatch.setattr(web_extraction.fields, 'extract', later_occurrence)
    claims = web_extraction.rule_claims([{'text': piece, 'offset': offset}])
    start = offset + len(repeated) + 1
    assert {claim['field'] for claim in claims} == {'bath_composition', 'cold_bath_temp_c'}
    assert all(claim['locator']['char_offsets'] == [start, start + len(repeated)] for claim in claims)


def test_rules_only_replay_and_upgrade_keep_claim_ids_and_candidate_links(case, monkeypatch):
    settings, path, url, pages, clock = case
    pages[url] += '\n샤워실 이용 가능'
    # Simulate a field being omitted by the older rule pass while present on the page.
    fallback = web_extraction.rule_claims
    monkeypatch.setattr(web_extraction, 'rule_claims',
                        lambda docs: [row for row in fallback(docs) if row['field'] != 'showers'])
    down = Session(Response(429, b'{}'))
    first = run_web(settings, targets_path=path, clock=clock, testing=True, qwen_session=down)
    assert first['counts']['claims'] == 4
    second = run_web(settings, targets_path=path, clock=clock, testing=True, qwen_session=down)
    assert len(down.calls) == 2
    assert (second['state'], second['reason'], second['collected']) == ('partial', 'model_rate_limited', False)
    assert [second['counts'][key] for key in ('claims', 'committed', 'unchanged', 'pending', 'rules_fallback')] == [
        4, 0, 1, 0, 1,
    ]
    review_policy = policy(settings, approval(url, venue_id='spa-0000', venue_key='spa-0000'))
    initial = run_promote(settings, policy_path=review_policy, clock=clock)
    initial_facts = {row['field']: row for row in initial['facts']}
    assert set(initial_facts) == {'bath_composition', 'cold_bath_temp_c', 'price_day_pass', 'tattoo_policy'}
    with closing(connect_readonly(settings.data_dir)) as db:
        originals = {claim['field']: claim for claim in read_claims(db)}
        original_rows = {row['field']: tuple(row) for row in db.execute('SELECT * FROM claims')}
        receipt = tuple(db.execute('SELECT * FROM receipts').fetchone())
    answers = [
        {'field': 'price_day_pass', 'value': PRICE, 'quote': '1일권 20,000원'},
        {'field': 'showers', 'value': True, 'quote': '샤워실 이용 가능'},
    ]
    model = WebModel(lambda items, _: _envelope({'items': [{'index': 0, 'claims': answers}]}))
    clock.sleep(91 * 86400)
    recovered = run_web(settings, targets_path=path, clock=clock, testing=True, qwen_session=model)
    assert recovered['success'] and recovered['collected'] and len(model.calls) == 1
    assert [recovered['counts'][key] for key in ('claims', 'committed', 'pending', 'rules_fallback')] == [1, 1, 0, 0]
    with closing(connect_readonly(settings.data_dir)) as db:
        current = {claim['field']: claim for claim in read_claims(db)}
        historical = {claim['field']: claim for claim in read_claims(db, include_superseded=True)}
        assert set(current) == {'price_day_pass', 'showers'} and len(historical) == 5
        assert {row['field']: tuple(row) for row in db.execute(
            "SELECT * FROM claims WHERE extraction_method='rules'")} == original_rows
        confirmed = current['price_day_pass']
        assert confirmed['claim_id'] == originals['price_day_pass']['claim_id']
        assert confirmed['reconciliation'] == 'model_confirmed' and confirmed['model_confirmed']
        assert confirmed['extraction_method'] == 'rules' and eligible(confirmed)
        assert current['showers']['extraction_method'] == 'model' and current['showers']['model_confirmed']
        omitted = historical['tattoo_policy']
        assert omitted['claim_id'] == originals['tattoo_policy']['claim_id']
        assert omitted['superseded'] and omitted['superseded_at'] == confirmed['confirmed_at']
        assert not eligible(omitted)
        upgraded = tuple(db.execute('SELECT * FROM receipts').fetchone())
        assert upgraded[:4] == receipt[:4] and upgraded[4] == 'applied' and upgraded[5:] == receipt[5:]
        observation = json.loads(db.execute('SELECT values_json FROM web_change_observations').fetchone()[0])
        assert [row['field'] for row in observation] == ['price_day_pass']
    reviewed = run_report(settings, policy_path=review_policy, clock=clock)
    price = next(row for row in reviewed['claims'] if row['field'] == 'price_day_pass')
    assert price['freshness'] == 'fresh' and price['checked'] == price['confirmed_at'][:10]
    facts = {row['field']: row for row in reviewed['facts']}
    assert facts['price_day_pass']['current_supporting_claim_ids'] == [confirmed['claim_id']]
    assert facts['price_day_pass']['candidate_id'] == initial_facts['price_day_pass']['candidate_id']
    assert 'tattoo_policy' not in facts
    held = next(row for row in reviewed['fact_candidates'] if row['field'] == 'tattoo_policy')
    assert held['review_state'] == 'evidence_hold' and held['supporting_claim_ids'] == [omitted['claim_id']]
    with closing(sqlite3.connect(settings.active_dir / 'collect-runtime.sqlite3')) as db:
        assert db.execute('SELECT count(*) FROM extraction_cache').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM web_completions').fetchone()[0] == 1
        assert not db.execute('SELECT 1 FROM extraction_retries').fetchone()
    clock.sleep(86400)
    cached = run_web(settings, targets_path=path, clock=clock, testing=True, qwen_session=model)
    assert cached['counts']['unchanged'] == 1 and len(model.calls) == 1


def test_binding_change_does_not_upgrade_or_relabel_rule_claims(case):
    settings, path, url, _, clock = case
    run_web(settings, targets_path=path, clock=clock, testing=True, qwen_session=Session(Response(429, b'{}')))
    review_policy = policy(settings, approval(url, venue_id='spa-0000', venue_key='spa-0000'))
    before = run_report(settings, policy_path=review_policy, clock=clock)
    targets = json.loads(path.read_text())
    targets['targets'][0].update(venue_id='another-fixture', venue_key='another-fixture')
    path.write_text(json.dumps(targets))
    model = WebModel(lambda items, _: _envelope({'items': [{'index': 0, 'claims': []}]}))
    result = run_web(settings, targets_path=path, clock=clock, testing=True, qwen_session=model)
    assert len(model.calls) == 1 and result['reason'] == 'source_binding_changed'
    assert result['counts']['pending'] == 1 and result['counts']['rules_fallback'] == 0
    after = run_report(settings, policy_path=review_policy, clock=clock)
    assert after['claims'] and not after['facts']
    for old, new in zip(before['claims'], after['claims'], strict=True):
        assert {k: new[k] for k in ('claim_id', 'venue_id', 'source_url', 'quote', 'value')} == {
            k: old[k] for k in ('claim_id', 'venue_id', 'source_url', 'quote', 'value')}
        assert new['binding_current'] == 0 and 'source_binding_changed' in new['eligibility_reasons']


def test_discovered_child_rule_evidence_stays_unverified(case):
    settings, path, url, pages, clock = case
    child = url.rsplit('/', 1)[0] + '/guide'
    pages[url] = 'Visitor information for the facilities. ' * 5 + '<a href="/guide">이용가이드</a>'
    pages[child] = MATCHING_PAGE
    result = run_web(settings, targets_path=path, clock=clock, testing=True,
                     qwen_session=Session(Response(429, b'{}')))
    assert result['counts']['rules_fallback'] == 1 and result['counts']['pending'] == 1
    review_policy = policy(settings, approval(child, venue_id='spa-0000', venue_key='spa-0000'))
    report = run_report(settings, policy_path=review_policy, clock=clock)
    assert report['state'] == 'ok' and len(report['claims']) == 4 and not report['facts']
    for claim in report['claims']:
        assert claim['source_url'] == child and claim['source_type'] == 'third_party'
        assert claim['source_authorization'] is None and claim['verification_label'] == '미확인'


def test_recovery_after_receipt_commit_with_no_runtime_index(case, monkeypatch):
    settings, path, _, _, clock = case

    def interrupt(*_args):
        raise CollectError('runtime_error')

    down = Session(Response(429, b'{}'))
    model = WebModel(lambda items, _: _envelope({'items': [{'index': 0, 'claims': []}]}))
    with monkeypatch.context() as interrupted:
        interrupted.setattr(web_extraction.WebExtraction, '_stored', interrupt)
        first = run_web(settings, targets_path=path, clock=clock, testing=True, qwen_session=down)
        assert first['state'] == 'partial' and first['counts']['pending'] == 1
        assert first['counts']['claims'] == 4
        with closing(connect_readonly(settings.data_dir)) as db:
            assert db.execute('SELECT disposition FROM receipts').fetchone()[0] == 'rules_only'
        recovered = run_web(settings, targets_path=path, clock=clock, testing=True, qwen_session=model)
        assert recovered['state'] == 'error' and len(model.calls) == 1
    result = run_web(settings, targets_path=path, clock=clock, testing=True, qwen_session=model)
    assert result['success'] and result['counts']['unchanged'] == 1
    assert not result['collected'] and len(model.calls) == 1
    with closing(connect_readonly(settings.data_dir)) as db:
        assert db.execute('SELECT disposition FROM receipts').fetchone()[0] == 'empty_extraction'
        assert not read_claims(db)
        old = read_claims(db, include_superseded=True)
        assert len(old) == 4 and all(claim['superseded'] for claim in old)
        assert json.loads(db.execute('SELECT values_json FROM web_change_observations').fetchone()[0]) == []


def test_record_rules_reconciliation_matrix(tmp_path):
    url = 'https://source.example.test/visit'
    at = kst_now(FakeClock()).isoformat()
    rule = web_extraction.rule_claims([{'text': '1일권 20,000원', 'offset': 0}])[0]
    auth = {'source_identity': url, 'requested_url': url, 'final_url': url, 'target_fingerprint': digest(url)}
    row = dict(kind='web', url=url, text_hash=digest('fixture'), venue_id='sample', venue_key='sample',
               final_url=url, hash_version=FULL_HASH_VERSION, seen_at=at, authorization=auth,
               domains=['example.test'])
    with closing(connect(tmp_path / 'ig')) as db:
        assert record(db, **row, disposition='rules_only', claims=[]) == ('no_rule_claims', [])
        assert not db.execute('SELECT 1 FROM receipts').fetchone()
        disposition, ids = record(db, **row, disposition='rules_only', claims=[rule])
        assert disposition == 'rules_only' and len(ids) == 1
        assert record(db, **row, disposition='rules_only', claims=[rule]) == ('duplicate', [])
        for changes in ({'venue_id': 'another-fixture'}, {'venue_key': 'another-fixture'},
                        {'final_url': url + '/change'}, {'authorization': None}, {'hash_version': None}):
            assert record(db, **(row | changes), disposition='applied', claims=[rule]) == ('binding_changed', [])
            assert not db.execute('SELECT 1 FROM claim_reconciliations').fetchone()
        assert record(db, **row, disposition='applied', claims=[rule]) == ('applied', [])
        assert record(db, **row, disposition='rules_only', claims=[rule]) == ('duplicate', [])
        assert record(db, **row, disposition='applied', claims=[]) == ('duplicate', [])
        claim = read_claims(db)[0]
        assert claim['claim_id'] == ids[0] and claim['reconciliation'] == 'model_confirmed'
        for statement in ('UPDATE claim_reconciliations SET at=at', 'DELETE FROM claim_reconciliations'):
            with pytest.raises(sqlite3.IntegrityError, match='immutable_reconciliation'), db:
                db.execute(statement)


def test_facts_only_rules_keep_grounding_and_do_not_reach_twin(case):
    from hanset_twin.daemon import run_once
    from hanset_twin.store import connect as twin_connect

    settings, path, url, pages, clock = case
    with closing(active_connect(settings.active_dir / 'active.sqlite3')) as active:
        result = run_web(settings, targets_path=path, clock=clock, testing=True, admission=SimpleNamespace(db=active),
                         qwen_session=Session(Response(429, b'{}')))
        assert result['counts']['rules_fallback'] == 1
        with closing(connect_readonly(settings.data_dir)) as db:
            claims = read_claims(db)
            assert len(claims) == 4
            expected = {claim['field']: claim for claim in web_extraction.rule_claims(
                list(web_extraction.fragments(web_extraction.page_text(pages[url]))))}
            for claim in claims:
                rule = expected[claim['field']]
                assert claim['quote'] == '' and claim['source_url'] == url
                assert claim['locator'] == {
                    'char_offsets': rule['locator']['char_offsets'],
                    'grounding': {'version': 1, 'span_hash': digest(rule['quote']), 'value_literal': False},
                }
            assert '1일권 20,000원'.encode() not in db.serialize()
        for table in ('raw_documents', 'observations', 'active_evidence_ready'):
            assert not active.execute(f'SELECT 1 FROM {table}').fetchone()
        with closing(twin_connect(':memory:')) as twin:
            _, state, counts = run_once(twin, active, csv_path=path.with_name('absent.csv'),
                                        lock_path=settings.twin_dir / 'daemon.lock', clock=clock,
                                        structured_only=True)
            assert state == 'ok' and counts['incremental_fetch'] == 0
            assert not twin.execute('SELECT 1 FROM facts').fetchone()
