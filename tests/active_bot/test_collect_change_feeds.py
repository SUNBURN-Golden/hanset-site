"""Proposal safety, deterministic exports and private offline operations."""
import json
import os
from pathlib import Path

import pytest

from hanset_active.collect import change_feeds as feeds, changes
from hanset_active.collect.config import CollectSettings
from hanset_active.collect.service import _review_result, run_detect_changes
from hanset_active.collect.targets import canonical_requested_url
from tests.active_bot.test_collect_changes import FIXTURES, TARGET, URL, events, observe, store


def exported(db, root, **kwargs):
    result = feeds.export_changes(db, root, **kwargs)
    assert result['reason'] == 'ok'
    assert feeds.manifest_status(root)['state'] == 'ok'
    folder = root/'official-web-changes'
    return {p.name: p.read_bytes() for p in folder.iterdir()}


def test_exports_are_deterministic_private_and_ids_preserved(store):
    db, root = store
    observe(db)
    observe(db, value=FIXTURES['hours']['after'], day=2)
    first = exported(db, root)
    second = exported(db, root)
    assert first == second
    manifest = json.loads(first['manifest.json'])
    assert manifest['event_watermark'] == 1 and manifest['as_of'] == '2026-11-02'
    for artifact in manifest['artifacts']:
        data = first[artifact['name']]
        assert feeds.sha(data) == artifact['sha256'] and len(data) == artifact['bytes']
    topic = json.loads(first['viral_topics.jsonl'])
    event = events(db)[0]
    assert topic['change_id'] == event['change_id']
    assert topic['requires_review'] and topic['auto_publish_to_site'] is False
    assert '09:00-18:00' in topic['blurb'] and topic['source_url'] == URL
    assert _review_result({'change_ids':[event['change_id']], 'target_fingerprint':event['target_fingerprint']})['change_ids'] == [event['change_id']]
    folder = root/'official-web-changes'
    assert folder.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in folder.iterdir())
    assert all(b'created_at' not in data and b'updated_at' not in data for data in first.values())
    (folder/'viral_topics.jsonl').write_bytes(b'altered\n')
    assert feeds.manifest_status(root)['state'] == 'invalid'


@pytest.mark.parametrize('statement', ['연중무휴', 'open_every_day', 'normally open'])
def test_no_closure_never_becomes_closed(store, statement):
    db, root = store
    observe(db, 'closures')
    observe(db, 'closures', statement, 2)
    files = exported(db, root)
    proposal = json.loads(files['closed_days.proposals.json'])['proposals'][0]
    assert proposal['state'] == 'no_closure' and proposal['action'] == 'review_only'
    assert 'closed' not in proposal and 'until' not in proposal
    assert files['viral_topics.jsonl'] == b''
    assert events(db)[0]['review_reason'] == 'no_closure'


@pytest.mark.parametrize('notice,until', [
    (FIXTURES['holiday'], '2026-12-03'),
    (FIXTURES['ambiguous_holiday'], None),
    ('Closed until 2026-12-03', '2026-12-03'),
    ('Closed 12/03', None),
    ('매월 2026-12-03 휴무', None),
    ('2026-12-01 ~ 2026-12-03 closed; 2026-12-05 closed', None),
    ('2026-12-01 ~ 2026-12-03 closed; 12/05 closed', None),
])
def test_closure_end_dates_are_conservative(store, notice, until):
    db, root = store
    observe(db, 'closures')
    observe(db, 'closures', notice, 2)
    files = exported(db, root)
    row = json.loads(files['closed_days.proposals.json'])['proposals'][0]
    assert row['closed'] == notice and row['action'] == 'update'
    assert row.get('until') == until
    if until is None:
        assert 'until' not in row
    assert row['site_slug'] is None and row['mapping_status'] == '확인 중'


def test_source_conflicts_block_topics_and_closure_updates(store):
    db, root = store
    other_url = 'https://other.example/hours'
    target = {**TARGET, 'urls':[URL, other_url], 'domains':{'venue.example', 'other.example'}}
    observe(db, 'closures', target=target)
    observe(db, 'closures', target=target, url=other_url)
    observe(db, 'closures', FIXTURES['closures']['after'], 2, target=target)
    assert events(db)[0]['review_reason'] == 'source_conflict'
    files = exported(db, root)
    row = json.loads(files['closed_days.proposals.json'])['proposals'][0]
    assert row['action'] == 'review_only' and row['review_reason'] == 'source_conflict'
    assert 'closed' not in row and files['viral_topics.jsonl'] == b''


def test_within_observation_conflict_preserves_baseline(store):
    from hanset_active.collect.store import read_claims
    db, _ = store
    observe(db)
    before = read_claims(db)[0]
    claims = [before, {**before, 'value': FIXTURES['hours']['after']}]
    observe(db, day=2, claims_override=claims)
    assert [e['review_reason'] for e in events(db)] == ['within_observation_conflict']
    assert events(db)[0]['new_value_json'] is None
    assert changes.change_report(db)['diagnostics'] == {'within_observation_conflict':1}


def test_explicit_mapping_and_staleness(store):
    db, root = store
    observe(db)
    observe(db, value=FIXTURES['hours']['after'], day=2)
    (root/'site_slug_map.json').write_text(json.dumps({TARGET['venue_id']:{'venue_key':TARGET['venue_key'],'site_slug':'example'}}))
    files = exported(db, root, as_of='2027-03-01')
    candidate = json.loads(files['rebuild_candidates.json'])['candidates'][0]
    assert candidate['suggested_routes'] == ['/finder/example/']
    assert candidate['freshness'] == '오래됨'
    assert files['viral_topics.jsonl'] == b''


def test_export_failure_retains_events_and_previous_valid_manifest(store, monkeypatch):
    db, root = store
    observe(db)
    observe(db, value=FIXTURES['hours']['after'], day=2)
    original = exported(db, root)
    observe(db, day=3)
    replace = os.replace
    calls = 0
    def fail_once(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('injected')
        return replace(src, dst)
    with monkeypatch.context() as patch:
        patch.setattr(feeds.os, 'replace', fail_once)
        assert feeds.export_changes(db, root)['reason'] == 'export_failed'
    assert len(events(db)) == 2
    assert feeds.manifest_status(root)['state'] == 'ok'
    assert original == {p.name:p.read_bytes() for p in (root/'official-web-changes').iterdir()}
    newer = exported(db, root)
    assert json.loads(newer['manifest.json'])['event_watermark'] == 2


@pytest.mark.parametrize('component', ['root','file'])
def test_symlink_outputs_rejected(store, tmp_path, component):
    db, root = store
    observe(db)
    observe(db, value=FIXTURES['hours']['after'], day=2)
    outside = tmp_path/'elsewhere'
    outside.mkdir()
    if component == 'root':
        (root/'official-web-changes').symlink_to(outside, target_is_directory=True)
    else:
        (root/'official-web-changes').mkdir()
        (root/'official-web-changes'/'manifest.json').symlink_to(outside/'manifest.json')
    assert feeds.export_changes(db, root)['reason'] == 'export_failed'
    assert list(outside.iterdir()) == []
    assert len(events(db)) == 1


@pytest.mark.parametrize('url', [
    'https://' + 'user:password' + '@' + 'venue.example/hours', 'https://venue.example/hours?token=sample',
    'https://venue.example/[token]', 'https://venue.example/%5Bredacted%5D',
    'https://venue.example/hours?password=sample',
    'https://localhost/hours', 'https://instagram.com/venue/',
])
def test_unsafe_urls_are_rejected_instead_of_sanitized(url):
    with pytest.raises(ValueError):
        canonical_requested_url(url)


def test_public_collection_url_is_ineligible_for_official_feed(store):
    db, _ = store
    observe(db)
    observe(db, value=FIXTURES['hours']['after'], day=2)
    event = dict(events(db)[0])
    url = 'https://naver.com/seed-spa-a'
    assert canonical_requested_url(url) == url and feeds._safe_event(event)
    assert not feeds._safe_event({**event, 'requested_url': url, 'final_url': url})


def test_unsafe_free_text_is_held_and_secrets_never_exported(store, monkeypatch):
    db, root = store
    observe(db, 'closures')
    # Construct contact-like synthetic input without placing contact details in fixtures.
    contact = 'example' + '@' + 'venue.example'
    observe(db, 'closures', 'Closed Tuesday; ' + contact, 2)
    assert [e['review_reason'] for e in events(db)] == ['unsafe_evidence']
    assert events(db)[0]['new_value_json'] is None and not events(db)[0]['usable_for_viral']
    assert changes.change_report(db)['diagnostics'] == {'unsafe_evidence':1}
    observe(db, 'closures', FIXTURES['closures']['after'], 3)
    files = exported(db, root)
    assert contact.encode() not in b''.join(files.values())
    assert all(b'API_KEY' not in value for value in files.values())


def test_offline_cli_ignores_model_config_and_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    from hanset_active.cli import main
    monkeypatch.setenv('HANSET_IG_DIR', str(tmp_path/'missing'))
    monkeypatch.setenv('HANSET_DEEPSEEK_MODEL', 'unknown')
    for args in (['detect-changes','--dry-run'], ['detect-changes','--dry-run','--rebaseline'],
                 ['report'], ['export-changes']):
        assert main(['collect', *args]) == 0
        assert json.loads(capsys.readouterr().out)['state'] == 'ok'
    assert not (tmp_path/'missing').exists()


def test_latest_conflicting_observation_holds_previous_clean_topics(store):
    from hanset_active.collect.store import read_claims
    db, root = store
    observe(db, 'closures')
    observe(db, 'closures', FIXTURES['closures']['after'], 2)
    assert exported(db, root)['viral_topics.jsonl']
    claims = [c for c in read_claims(db) if c['field'] == 'closures']
    observe(db, 'closures', day=3, claims_override=claims)
    files = exported(db, root)
    assert files['viral_topics.jsonl'] == b''
    row = json.loads(files['closed_days.proposals.json'])['proposals'][0]
    assert row['action'] == 'review_only' and 'closed' not in row
    assert row['review_reason'] == 'within_observation_conflict'


def test_removed_authorization_retires_feed_scope_without_a_new_fetch(store):
    db, root = store
    observe(db)
    observe(db, value=FIXTURES['hours']['after'], day=2)
    assert exported(db, root)['viral_topics.jsonl']
    changes.process_pending(db, [])
    files = exported(db, root)
    assert files['viral_topics.jsonl'] == b''
    assert json.loads(files['rebuild_candidates.json'])['candidates'] == []
    assert changes.metadata(db)['authorization_retired_baselines'] == '1'
    assert len(events(db)) == 1


def test_unsafe_qualifiers_are_not_stored_in_observations_or_events(store):
    from hanset_active.collect.store import read_claims
    db, _ = store
    observe(db)
    contact = 'example' + '@' + 'venue.example'
    claim = {**read_claims(db)[0], 'value':{'hours':'10:00-18:00','scope':contact}, 'quote':'10:00-18:00'}
    observe(db, day=2, claims_override=[claim])
    assert contact not in db.execute('SELECT values_json FROM web_change_observations ORDER BY observed_seq DESC').fetchone()[0]
    assert events(db) == []
