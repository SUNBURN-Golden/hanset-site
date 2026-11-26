"""Synthetic external databases only. No sources, models or site output."""
from contextlib import closing
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from hanset_active.collect import CollectError
from hanset_active.collect.config import CollectSettings
from hanset_active.collect.promotion_policy import load_policy
from hanset_active.collect.service import run_promote, run_report, run_web
from hanset_active.collect.store import connect, digest, record
from hanset_active.db import connect as active_connect
from hanset_active.util.timeutil import FakeClock
from tests.active_bot.test_collect_provenance import OLD_SCHEMA

SEEN = '2026-11-09T12:00:00+09:00'
A, B, C = 'https://alpha.test/hours', 'https://beta.test/hours', 'https://gamma.test/hours'
IG_A, IG_B = 'https://www.instagram.com/p/SampleA/', 'https://www.instagram.com/p/SampleB/'
HOURS = {'hours': '09:00-17:00', 'scope': 'weekday'}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for name in ('DEEPSEEK_API_KEY', 'TAVILY_API_KEY', 'HANSET_DEEPSEEK_MODEL', 'HANSET_COLLECT_PROMOTION_POLICY'):
        monkeypatch.delenv(name, raising=False)
    settings = CollectSettings(data_dir=tmp_path / 'ig', active_dir=tmp_path / 'active', twin_dir=tmp_path / 'twin')
    clock = FakeClock(datetime.fromisoformat('2026-11-10T12:00:00+09:00'))
    return settings, clock


def approval(url, *, group=None, **extra):
    host = url.split('/')[2]
    domain = '.'.join(host.split('.')[-2:])
    instagram = domain == 'instagram.com'
    return {'venue_id': 'sample', 'venue_key': 'sample', 'source_url': url,
            'source_kind': 'instagram' if instagram else 'web', 'registrable_domain': domain,
            'domain_boundary_reviewed': True, 'document_origin_group': group or domain,
            'approved_at': '2026-11-09', **({'official_handle': 'sample.venue'} if instagram else {}), **extra}


def policy(settings, *rows):
    path = settings.data_dir.parent / 'review-policy.json'
    path.write_text(json.dumps({'schema_version': 1, 'approvals': list(rows)}))
    return path


def add(settings, url, *, value=None, seen=SEEN, text_hash=None, field='hours', source_type=None, **identity):
    db = connect(settings.data_dir)
    try:
        _, ids = record(db, kind='instagram' if 'instagram.com' in url else 'web', url=url,
                        text_hash=digest(text_hash or 'synthetic-' + url + json.dumps(value)), disposition='applied',
                        claims=[{'field': field, 'value': value if value is not None else HOURS,
                                 'quote': 'SAMPLE minimal evidence', 'rule_id': 'sample.v1'}],
                        venue_id=identity.get('venue_id', 'sample'), venue_key=identity.get('venue_key', 'sample'),
                        seen_at=seen, source_type=source_type)
        return ids[0]
    finally:
        db.close()


def persisted(settings):
    db = sqlite3.connect(settings.data_dir / 'collector.sqlite3')
    try:
        return {table: db.execute('SELECT * FROM ' + table + ' ORDER BY 1,2').fetchall()
                for table in ('claims', 'receipts', 'fact_candidates', 'fact_candidate_claims')}
    finally:
        db.close()


def test_agree_rerun_corroboration_and_revocation(setup):
    settings, clock = setup
    a = add(settings, A)
    b = add(settings, B, value={'hours': '9:00 – 17:00', 'scope': 'weekday'})
    path = policy(settings, approval(A), approval(B), approval(C))
    first = run_promote(settings, policy_path=path, clock=clock)
    assert first['state'] == 'ok' and first['auto_publish_to_site'] is False
    candidate = first['fact_candidates'][0]
    assert candidate['review_state'] == 'awaiting_review'
    assert first['facts'][0]['supporting_claim_ids'] == sorted([a, b])
    assert candidate['qualification_basis'] == 'independent_agreement'
    assert candidate['status'] == 'fact_candidate' and candidate['rights'] == 'internal_review_only'
    assert candidate['supporting_claim_ids'] == sorted([a, b]) and candidate['independence_rationale']
    before = persisted(settings)
    clock.sleep(60)
    repeat = run_promote(settings, policy_path=path, clock=clock)
    assert repeat['changed_candidates'] == 0 and persisted(settings) == before
    c = add(settings, C)
    corroborated = run_promote(settings, policy_path=path, clock=clock)
    assert corroborated['fact_candidates'][0]['candidate_id'] == candidate['candidate_id']
    assert set(corroborated['fact_candidates'][0]['supporting_claim_ids']) == {a, b, c}
    assert len(persisted(settings)['fact_candidates']) == 1
    policy(settings, approval(A))
    readonly = run_report(settings, policy_path=path, clock=clock)
    assert readonly['fact_candidates'][0]['review_state'] == 'evidence_hold'
    assert persisted(settings)['fact_candidates'][0][9] == 'awaiting_review'
    held = run_promote(settings, policy_path=path, clock=clock)
    assert held['fact_candidates'][0]['review_state'] == 'evidence_hold'
    assert held['facts'] == [] and held['fact_candidates'][0]['current_supporting_claim_ids'] == []
    assert set(held['fact_candidates'][0]['supporting_claim_ids']) == {a, b, c}
    assert not settings.active_dir.exists() and not settings.twin_dir.exists()


@pytest.mark.parametrize('urls,expected', [((A, B), 1), ((A, IG_A), 1), ((IG_A, IG_B), 0)])
def test_official_web_required(setup, urls, expected):
    settings, clock = setup
    for url in urls:
        add(settings, url)
    path = policy(settings, *(approval(url) for url in urls))
    report = run_promote(settings, policy_path=path, clock=clock)
    assert report['state'] == 'ok' and len(report['fact_candidates']) == expected


@pytest.mark.parametrize('kind', ['single', 'shared_host', 'mirror', 'same_origin', 'independent'])
def test_review_only_web_approvals_require_independent_agreement(setup, kind):
    settings, clock = setup
    first = 'https://blog.naver.com/seed-spa-a'
    second = 'https://blog.naver.com/box-b' if kind == 'shared_host' else 'https://runport.example.test/hours'
    urls = [first] if kind == 'single' else [first, second]
    for url in urls:
        add(settings, url, source_type='third_party', text_hash='same-document' if kind == 'mirror' else None)
    path = policy(settings, *(approval(url, review_only=True,
                                       group='shared-origin' if kind == 'same_origin' else url)
                              for url in urls))
    report = run_promote(settings, policy_path=path, clock=clock)
    assert report['state'] == 'ok' and report['auto_publish_to_site'] is False
    assert all(claim['verification_label'] == '미확인' for claim in report['claims'])
    assert len(report['fact_candidates']) == int(kind == 'independent')
    if kind == 'independent':
        assert report['facts'][0]['qualification_basis'] == 'independent_agreement'
        assert report['facts'][0]['status'] == 'fact'
    else:
        assert report['facts'] == []


@pytest.mark.parametrize('row', [approval(IG_A, review_only=True), approval(IG_A, review_only=False),
                                approval(A, review_only='true'),
                                approval('https://naver.com/seed-spa-a'),
                                approval('https://instagr.am/seed-spa-a', review_only=True)])
def test_review_only_policy_rejects_invalid_approval_classes(setup, row):
    settings, clock = setup
    with pytest.raises(CollectError, match='promotion_policy_invalid'):
        load_policy(policy(settings, row), today=clock.now().date())


@pytest.mark.parametrize('variant', ['same_page', 'canonical_alias', 'subdomain', 'same_domain', 'mirror', 'translation', 'same_hash', 'ambiguous_approval', 'wrong_venue'])
def test_duplicate_or_unapproved_sources_cannot_vote_twice(setup, variant):
    settings, clock = setup
    second = B
    if variant == 'same_page': second = A
    if variant == 'canonical_alias': second = A + '?utm_source=sample'
    if variant == 'subdomain': second = 'https://news.alpha.test/hours'
    if variant == 'same_domain': second = 'https://alpha.test/prices'
    add(settings, A, text_hash='first')
    add(settings, second, text_hash='first' if variant == 'same_hash' else 'second')
    rows = [approval(A), approval(second)]
    if variant == 'same_page': rows = [approval(A)]
    if variant in ('mirror', 'translation'): rows[1]['document_origin_group'] = rows[0]['document_origin_group']
    if variant == 'ambiguous_approval': rows.append(approval(B))
    if variant == 'wrong_venue': rows[1]['venue_key'] = 'another'
    result = run_promote(settings, policy_path=policy(settings, *rows), clock=clock)
    assert result['state'] == 'ok' and result['fact_candidates'] == []
    assert len(result['claims']) == 2
    active_connect(settings.active_dir / 'active.sqlite3').close()
    (settings.data_dir / 'promotion_policy.json').write_bytes(policy(settings, *rows).read_bytes())
    result = run_web(settings, clock=clock, testing=True)
    assert result['state'] == 'no_targets' and result['promotion'] == {'reason': 'ok', 'changed_candidates': 0}
    assert persisted(settings)['fact_candidates'] == []


def test_conflicts_keep_revisions_and_rank_observed_instants_and_ties(setup):
    settings, clock = setup
    old = add(settings, A, value=HOURS, seen='2026-01-01T12:00:00+09:00', text_hash='old')
    latest = add(settings, A, value={**HOURS, 'hours': '10:00-18:00'}, seen='2026-11-09T05:00:00+00:00', text_hash='revision')
    tied = add(settings, B, value=HOURS, seen='2026-11-09T14:00:00+09:00')
    invalid = add(settings, C, value=HOURS, seen='invalid')
    result = run_promote(settings, policy_path=policy(settings, approval(A), approval(B)), clock=clock)
    conflict = result['conflicts'][0]
    assert conflict['state'] == '확인 필요' and conflict['date_basis'] == 'seen_at'
    assert set(conflict['claim_ids']) == {old, latest, tied, invalid}
    assert set(conflict['newest_claim_ids']) == {latest, tied}
    observations = {item['claim_id']: item for item in conflict['observations']}
    assert observations[old]['freshness'] == '오래됨'
    assert observations[invalid]['freshness'] == 'unranked' and observations[invalid]['is_newest'] is False
    assert result['fact_candidates'][0]['review_state'] == 'needs_confirmation'
    before = persisted(settings)
    clock.sleep(100 * 24 * 60 * 60)
    repeated = run_promote(settings, policy_path=policy(settings, approval(A), approval(B)), clock=clock)
    assert repeated['changed_candidates'] == 0 and persisted(settings) == before


def test_disagreement_alone_and_two_qualifying_values(setup):
    settings, clock = setup
    add(settings, A)
    add(settings, B, value={**HOURS, 'hours': '10:00-18:00'})
    path = policy(settings, approval(A), approval(B), approval(C), approval('https://delta.test/hours'))
    result = run_promote(settings, policy_path=path, clock=clock)
    assert len(result['conflicts']) == 1 and result['fact_candidates'] == []
    add(settings, C)
    add(settings, 'https://delta.test/hours', value={**HOURS, 'hours': '10:00-18:00'})
    result = run_promote(settings, policy_path=path, clock=clock)
    assert len(result['fact_candidates']) == 2
    assert {item['review_state'] for item in result['fact_candidates']} == {'needs_confirmation'}
    assert len({item['conflict_id'] for item in result['fact_candidates']}) == 1
    assert len(persisted(settings)['claims']) == 4


def test_legacy_readonly_migration_and_nulls_untouched(setup):
    settings, clock = setup
    settings.data_dir.mkdir()
    db = sqlite3.connect(settings.data_dir / 'collector.sqlite3')
    db.executescript(OLD_SCHEMA)
    db.execute('INSERT INTO receipts VALUES (?,?,?,?,?,?)', ('legacy', 'web', A, 'legacy', 'applied', SEEN))
    db.execute('INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               ('legacy', 'legacy', 'sample', 'sample', 'hours', json.dumps(HOURS), 'SAMPLE legacy', A,
                'web', 'own_site', 'source_claim', 'internal_review_only', 'sample', SEEN, 'legacy'))
    db.commit()
    before = db.execute('SELECT * FROM claims').fetchone()
    schema = db.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall()
    db.close()
    path = policy(settings, approval(A), approval(B))
    report = run_promote(settings, policy_path=path, dry_run=True, clock=clock)
    assert report['state'] == 'ok' and report['claims'][0]['evidence_origin'] is None
    assert report['claims'][0]['eligibility_reasons'] == ['legacy_provenance_unrecorded']
    assert report['claims'][0]['model_confirmed'] is True
    assert report['claims'][0]['extraction_method'] is None
    assert report['claims'][0]['reconciliation'] is None
    db = sqlite3.connect(settings.data_dir / 'collector.sqlite3')
    assert db.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall() == schema
    db.close()
    add(settings, B)
    active_connect(settings.active_dir / 'active.sqlite3').close()
    (settings.data_dir / 'promotion_policy.json').write_bytes(path.read_bytes())
    agreement = run_web(settings, clock=clock, testing=True)
    assert agreement['promotion'] == {'reason': 'ok', 'changed_candidates': 0}
    add(settings, C, value={**HOURS, 'hours': '10:00-18:00'})
    promoted = run_promote(settings, policy_path=path, clock=clock)
    assert promoted['fact_candidates'] == [] and promoted['conflicts'][0]['state'] == '확인 필요'
    db = connect(settings.data_dir)
    row = tuple(db.execute("SELECT * FROM claims WHERE claim_id='legacy'").fetchone())
    assert row[:len(before)] == before and row[len(before):] == (None,) * 6
    assert db.execute('SELECT COUNT(*) FROM fact_candidates').fetchone()[0] == 0
    db.close()


def test_readonly_missing_state_and_missing_policy(setup):
    settings, clock = setup
    path = policy(settings)
    assert run_promote(settings, policy_path=path, dry_run=True, clock=clock)['fact_candidates'] == []
    assert run_report(settings, clock=clock)['claims'] == []
    assert not settings.data_dir.exists()
    add(settings, A)
    add(settings, B)
    before = persisted(settings)
    assert run_report(settings, clock=clock)['fact_candidates'] == []
    assert persisted(settings) == before


@pytest.mark.parametrize('field,left,right,qualifier', [
    ('hours', HOURS, {**HOURS, 'hours': '9:00 – 17:00'}, {'scope': 'weekend'}),
    ('hours', {**HOURS, 'facility': 'sauna'}, {**HOURS, 'facility': 'sauna'}, {'facility': 'pool'}),
    ('price_day_pass', {'amount': 28000, 'ticket': 'day_pass', 'audience': 'adult'},
     {'amount': '28,000원', 'ticket': 'day_pass', 'audience': 'adult'}, {'audience': 'child'}),
])
def test_normalized_candidates_and_separate_qualifier_groups(setup, field, left, right, qualifier):
    settings, clock = setup
    add(settings, A, value=left, field=field)
    add(settings, B, value=right, field=field)
    path = policy(settings, approval(A), approval(B), approval(C))
    result = run_promote(settings, policy_path=path, clock=clock)
    candidate = result['fact_candidates'][0]
    add(settings, C, value={**left, **qualifier}, field=field)
    result = run_promote(settings, policy_path=path, clock=clock)
    assert len(result['fact_candidates']) == 1 and result['conflicts'] == []
    assert result['fact_candidates'][0]['candidate_id'] == candidate['candidate_id']
    assert len(result['fact_candidates'][0]['supporting_claim_ids']) == 2


def test_invalid_values_and_all_invalid_dates_remain_visible(setup):
    settings, clock = setup
    add(settings, A, seen='2026-11-09T12:00:00')
    add(settings, B, value={**HOURS, 'hours': '10:00-18:00'}, seen='invalid')
    add(settings, C, value={**HOURS, 'hours': {'invalid': 'shape'}}, seen='')
    result = run_report(settings, clock=clock)
    assert result['state'] == 'ok' and len(result['claims']) == 3
    assert result['conflicts'][0]['newest_claim_ids'] == []
    assert not any(item['is_newest'] for item in result['conflicts'][0]['observations'])
    assert result['eligibility_reason_counts']['unsupported_or_ambiguous_value'] == 1


def test_order_determinism_and_model_network_boundary(setup, monkeypatch):
    import socket
    from hanset_active.collect import service
    from hanset_active.collect.promotion import assess
    from hanset_active.collect.store import read_claims
    settings, clock = setup
    add(settings, A)
    add(settings, B)
    path = policy(settings, approval(A), approval(B))

    def forbidden(*args, **kwargs):
        raise AssertionError('unexpected external system access')

    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(service.DeepSeek, 'ask', forbidden)
    monkeypatch.setattr(service, 'active_connect', forbidden)
    monkeypatch.setattr(service, 'State', forbidden)
    assert run_promote(settings, policy_path=path, clock=clock)['state'] == 'ok'
    db = connect(settings.data_dir)
    claims = read_claims(db)
    db.close()
    approvals = load_policy(path)
    forward = assess(claims, [], approvals, now=clock.now())
    backward = assess(list(reversed(claims)), [], approvals, now=clock.now())
    assert forward == backward
    assert run_report(settings, clock=clock)['fact_candidates'][0]['review_state'] == 'evidence_hold'
    assert not settings.active_dir.exists() and not settings.twin_dir.exists()


def test_report_and_dry_run_preserve_database_bytes_and_permissions(setup):
    settings, clock = setup
    add(settings, A)
    add(settings, B)
    path = policy(settings, approval(A), approval(B))
    database = settings.data_dir / 'collector.sqlite3'
    before, metadata = database.read_bytes(), database.stat()
    assert run_report(settings, policy_path=path, clock=clock)['state'] == 'ok'
    assert run_promote(settings, policy_path=path, dry_run=True, clock=clock)['state'] == 'ok'
    after = database.stat()
    assert database.read_bytes() == before and after.st_mtime_ns == metadata.st_mtime_ns
    assert after.st_mode == metadata.st_mode


def test_known_domain_boundary_validation_and_attested_unknown_suffix(setup):
    settings, _clock = setup
    bad = policy(settings, approval('https://news.alpha.co.kr/hours', registrable_domain='news.alpha.co.kr'))
    with pytest.raises(CollectError):
        load_policy(bad)
    good = policy(settings, approval('https://news.alpha.co.kr/hours', registrable_domain='alpha.co.kr'),
                  approval('https://news.beta.co.nz/hours', registrable_domain='beta.co.nz'))
    assert len(load_policy(good)['approvals']) == 2


def test_collector_lock_defers_candidate_writes(setup):
    from hanset_twin.daemon import ProcessLock
    settings, clock = setup
    add(settings, A)
    add(settings, B)
    path = policy(settings, approval(A), approval(B))
    before = persisted(settings)
    with ProcessLock(settings.data_dir / 'daemon.lock'):
        result = run_promote(settings, policy_path=path, clock=clock)
    assert result['reason'] == 'already_running' and persisted(settings) == before


@pytest.mark.parametrize('change', [
    {'domain_boundary_reviewed': False}, {'registrable_domain': 'test'},
    {'source_url': 'https://naver.com/example'}, {'source_url': 'http://alpha.test/hours'},
    {'source_url': 'https://%6eaver.com/example'},
    {'source_url': 'https://127.0.0.1/hours'}, {'approved_at': 'not-a-date'},
    {'approved_at': '2099-01-01'}, {'source_kind': 'instagram'},
])
def test_policy_fails_closed(setup, change):
    settings, clock = setup
    path = policy(settings, approval(A, **change))
    with pytest.raises(CollectError, match='promotion_policy_invalid'):
        load_policy(path, today=clock.now().date())
    assert not settings.data_dir.exists()


def test_ig_requires_attested_handle_and_domain_review(setup):
    settings, clock = setup
    row = approval(IG_A)
    del row['official_handle']
    path = policy(settings, row)
    assert run_promote(settings, policy_path=path, clock=clock)['reason'] == 'promotion_policy_invalid'
    assert not settings.data_dir.exists()


def test_transaction_failure_rolls_back_all_candidates_and_links(setup, monkeypatch):
    from hanset_active.collect import store
    settings, clock = setup
    add(settings, A)
    add(settings, B)
    before = persisted(settings)
    persist = store.persist_candidates

    def fail(db, candidates, *, at):
        persist(db, candidates, at=at)
        raise sqlite3.IntegrityError('synthetic failure')

    monkeypatch.setattr(store, 'persist_candidates', fail)
    result = run_promote(settings, policy_path=policy(settings, approval(A), approval(B)), clock=clock)
    assert result['state'] == 'error' and 'synthetic failure' not in json.dumps(result)
    assert persisted(settings) == before


def test_cli_dry_run_and_report_without_credentials_or_network(setup):
    settings, _clock = setup
    add(settings, A)
    add(settings, B)
    path = policy(settings, approval(A, approved_at='2020-01-01'), approval(B, approved_at='2020-01-01'))
    before = persisted(settings)
    env = {**os.environ, 'HANSET_IG_DIR': str(settings.data_dir),
           'HANSET_ACTIVE_DIR': str(settings.active_dir), 'HANSET_TWIN_DIR': str(settings.twin_dir),
           'PYTHONDONTWRITEBYTECODE': '1', 'HANSET_DEEPSEEK_MODEL': 'irrelevant-to-review'}
    for args in (['promote', '--policy', str(path), '--dry-run'], ['report', '--policy', str(path)]):
        result = subprocess.run([sys.executable, '-m', 'hanset_active', 'collect', *args], env=env,
                                cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        body = json.loads(result.stdout)
        assert body['state'] == 'ok' and body['auto_publish_to_site'] is False
        assert len(body['fact_candidates']) == 1
    assert persisted(settings) == before
    assert not settings.active_dir.exists() and not settings.twin_dir.exists()


@pytest.mark.parametrize('same_content', (True, False))
def test_guarded_final_domain_official_qualification_and_rebinding(setup, monkeypatch, same_content):
    from hanset_active.fetcher import FetchResult
    from tests.active_bot.test_collect_deepseek import enable_qwen
    from tests.active_bot.test_collect_web import WebModel, _hours_answer

    settings, clock = setup
    enable_qwen(monkeypatch)
    settings.data_dir.mkdir()
    target = settings.data_dir / 'web_targets.json'
    final = 'https://hours.alpha.test/visit'
    entry = {'venue_id': 'sample', 'venue_key': 'sample', 'name': 'Seed Spa A', 'official_urls': [A]}
    target.write_text(json.dumps({'schema_version': 1, 'targets': [entry]}))
    body = ('<main>운영시간 06:00-19:00. ' + 'Official venue information. ' * 6 + '</main>').encode()
    monkeypatch.setattr('hanset_active.collect.service.Fetcher.fetch',
                        lambda self, url, **kwargs: FetchResult(final, 'ok', status=200,
                                                                content=body, content_type='text/html'))
    model = WebModel(lambda items, _: _hours_answer(items))
    collected = run_web(settings, targets_path=target, clock=clock, qwen_session=model, testing=True)
    assert collected['collected']
    path = policy(settings, approval(A))
    dry = run_promote(settings, policy_path=path, dry_run=True, clock=clock)
    promoted = run_promote(settings, policy_path=path, clock=clock)
    assert dry['facts'][0]['qualification_basis'] == promoted['facts'][0]['qualification_basis'] == 'official'
    fact = promoted['facts'][0]
    assert fact['status'] == 'fact' and fact['rights'] == 'internal_review_only'
    claim = promoted['claims'][0]
    assert claim['source_url'] == A and claim['final_url'] == final and claim['source_kind'] == 'web'
    assert claim['source_authorization']['final_url'] == final and claim['source_authorization']['requested_url'] == A
    assert fact['supporting_claim_ids'] == [claim['claim_id']]
    with sqlite3.connect(settings.data_dir / 'collector.sqlite3') as db:
        basis, ids = db.execute('SELECT qualification_basis,current_supporting_claim_ids_json FROM fact_candidates').fetchone()
        assert basis == 'official' and json.loads(ids) == [claim['claim_id']]
    entry.update(venue_id='box-b', venue_key='box-b', name='Box B')
    target.write_text(json.dumps({'schema_version': 1, 'targets': [entry]}))
    if not same_content:
        body = body.replace(b'06:00-19:00', b'06:00-20:00')
    clock.sleep(86400)
    collected = run_web(settings, targets_path=target, clock=clock, qwen_session=model, testing=True)
    assert collected['counts']['unchanged' if same_content else 'committed'] == 1
    assert len(model.calls) == (1 if same_content else 2)
    held = run_promote(settings, policy_path=path, clock=clock)
    assert held['facts'] == []
    assert held['fact_candidates'][0]['review_state'] == 'evidence_hold'
    original = next(item for item in held['claims'] if item['claim_id'] == claim['claim_id'])
    assert original['seen_at'] == claim['seen_at']


def test_already_complete_web_recovers_agreement_and_newer_conflict(setup, monkeypatch):
    from hanset_active.fetcher import FetchResult
    from tests.active_bot.test_collect_web import WebModel, _hours_answer, _page, _targets, enable_qwen

    settings, clock = setup
    active_connect(settings.active_dir / 'active.sqlite3').close()
    add(settings, A)
    add(settings, B)
    enable_qwen(monkeypatch)
    _targets(settings.data_dir / 'web_targets.json', C, venue_id='another', venue_key='another')
    body = _page().encode()
    monkeypatch.setattr('hanset_active.collect.service.Fetcher.fetch', lambda self, url, **kwargs:
                        FetchResult(url, 'ok', status=200, content=body, content_type='text/html'))
    answer = WebModel(lambda items, _: _hours_answer(items))

    def web():
        return run_web(settings, clock=clock, qwen_session=answer, testing=True)

    assert web()['promotion'] == {'reason': 'promotion_policy_absent'}
    default = settings.data_dir / 'promotion_policy.json'
    default.write_text('fixture-private-invalid-policy')
    assert web()['promotion'] == {'reason': 'promotion_policy_invalid'}
    assert persisted(settings)['fact_candidates'] == []
    path = policy(settings, approval(A), approval(B))
    monkeypatch.setenv('HANSET_COLLECT_PROMOTION_POLICY', str(path))
    recovered = web()
    assert recovered['state'] == 'already_complete'
    assert recovered['promotion'] == {'reason': 'ok', 'changed_candidates': 1}
    snapshot = persisted(settings)
    assert len(snapshot['fact_candidate_claims']) == 2
    assert web()['promotion'] == {'reason': 'ok', 'changed_candidates': 0}
    assert persisted(settings) == snapshot and len(answer.calls) == 1

    later = add(settings, B, value={**HOURS, 'hours': '10:00-18:00'}, seen='2026-11-10T10:00:00+09:00')
    assert web()['promotion'] == {'reason': 'ok', 'changed_candidates': 1}
    report = run_report(settings, policy_path=path, clock=clock)
    conflict, = report['conflicts']
    assert conflict['state'] == '확인 필요' and conflict['newest_claim_ids'] == [later]
    assert {value['hours'] for value in conflict['values']} == {'09:00-17:00', '10:00-18:00'}
    assert report['facts'] == [] and persisted(settings)['fact_candidates'][0][9] == 'needs_confirmation'
    policy(settings)
    assert web()['promotion'] == {'reason': 'ok', 'changed_candidates': 1}
    assert persisted(settings)['fact_candidates'][0][9] == 'evidence_hold'


def test_web_checks_current_official_binding_without_relabeling_or_using_adhoc_targets(setup, monkeypatch):
    from hanset_active.collect.plan import assemble_targets
    from hanset_active.collect.targets import source_authorization
    from hanset_active.fetcher import FetchResult
    from tests.active_bot.test_collect_web import _targets

    settings, clock = setup
    active_connect(settings.active_dir / 'active.sqlite3').close()
    settings.data_dir.mkdir()
    default = settings.data_dir / 'web_targets.json'
    _targets(default, A, venue_id='sample', venue_key='sample')
    target, = assemble_targets(settings, now=clock.now(), testing=True)['authorization']
    with closing(connect(settings.data_dir)) as db:
        record(db, kind='web', url=A, text_hash=digest(A), disposition='applied', venue_id='sample',
               venue_key='sample', seen_at=SEEN, claims=[{'field': 'hours', 'value': HOURS,
                                                         'quote': 'Sample evidence', 'rule_id': 'sample.v1'}],
               source_type='own_site', requested_url=A, final_url=A, domains=target['domains'],
               authorization=source_authorization(target, A, testing=True), testing=True)
    (default.parent / 'promotion_policy.json').write_bytes(policy(settings, approval(A)).read_bytes())
    monkeypatch.setattr('hanset_active.collect.service.Fetcher.fetch', lambda self, url, **kwargs:
                        FetchResult(url, 'forbidden', status=403))
    assert run_web(settings, clock=clock, testing=True)['promotion'] == {'reason': 'ok', 'changed_candidates': 1}
    before = persisted(settings)
    adhoc = default.with_name('adhoc.json')
    adhoc.write_text('{"schema_version": 1, "targets": []}')
    assert run_web(settings, targets_path=adhoc, clock=clock, testing=True)['promotion'] == {
        'reason': 'promotion_adhoc_targets'}
    assert persisted(settings) == before
    default.write_bytes(adhoc.read_bytes())
    assert run_web(settings, clock=clock, testing=True)['promotion'] == {'reason': 'ok', 'changed_candidates': 1}
    after = persisted(settings)
    assert after['fact_candidates'][0][9] == 'evidence_hold'
    assert after['claims'] == before['claims'] and after['receipts'] == before['receipts']
    assert after['fact_candidate_claims'] == before['fact_candidate_claims']


def test_rules_only_claim_requires_model_confirmation(setup):
    from hanset_active.collect.normalize import normalize_value
    from hanset_active.collect.promotion import _eligibility
    from hanset_active.collect.store import read_claims

    settings, _ = setup
    add(settings, A)
    db = connect(settings.data_dir)
    try:
        claim, = read_claims(db)
    finally:
        db.close()
    loaded = load_policy(policy(settings, approval(A)))
    claim['normalization'] = normalize_value(claim['field'], claim['value'])
    _, reasons = _eligibility({**claim, 'model_confirmed': False}, loaded)
    assert 'model_confirmation_required' in reasons
    _, confirmed = _eligibility({**claim, 'model_confirmed': True}, loaded)
    assert 'model_confirmation_required' not in confirmed
