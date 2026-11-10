"""Synthetic external databases only. No sources, models or site output."""
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
from hanset_active.collect.service import run_promote, run_report
from hanset_active.collect.store import connect, record
from hanset_active.util.timeutil import FakeClock
from tests.active_bot.test_collect_provenance import OLD_SCHEMA

SEEN = '2026-11-09T12:00:00+09:00'
A, B, C = 'https://alpha.test/hours', 'https://beta.test/hours', 'https://gamma.test/hours'
IG_A, IG_B = 'https://www.instagram.com/p/SampleA/', 'https://www.instagram.com/p/SampleB/'
HOURS = {'hours': '09:00-17:00', 'scope': 'weekday'}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for name in ('DEEPSEEK_API_KEY', 'TAVILY_API_KEY', 'HANSET_DEEPSEEK_MODEL'):
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


def add(settings, url, *, value=None, seen=SEEN, text_hash=None, field='hours', **identity):
    db = connect(settings.data_dir)
    try:
        _, ids = record(db, kind='instagram' if 'instagram.com' in url else 'web', url=url,
                        text_hash=text_hash or 'synthetic-' + url + json.dumps(value), disposition='applied',
                        claims=[{'field': field, 'value': value if value is not None else HOURS,
                                 'quote': 'SAMPLE minimal evidence', 'rule_id': 'sample.v1'}],
                        venue_id=identity.get('venue_id', 'sample'), venue_key=identity.get('venue_key', 'sample'), seen_at=seen)
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
    db = sqlite3.connect(settings.data_dir / 'collector.sqlite3')
    assert db.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall() == schema
    db.close()
    add(settings, B)
    agreement = run_promote(settings, policy_path=path, clock=clock)
    assert agreement['fact_candidates'] == [] and agreement['conflicts'] == []
    add(settings, C, value={**HOURS, 'hours': '10:00-18:00'})
    promoted = run_promote(settings, policy_path=path, clock=clock)
    assert promoted['fact_candidates'] == [] and promoted['conflicts'][0]['state'] == '확인 필요'
    db = connect(settings.data_dir)
    row = tuple(db.execute("SELECT * FROM claims WHERE claim_id='legacy'").fetchone())
    assert row[:len(before)] == before and row[len(before):] == (None, None, None)
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
