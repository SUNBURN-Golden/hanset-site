"""Daily observations, immutable transitions, provenance and recovery contracts."""
import json
import sqlite3
from pathlib import Path

import pytest

from hanset_active.collect import changes
from hanset_active.collect.config import CollectSettings
from hanset_active.collect.service import run_detect_changes, run_export_changes, run_report
from hanset_active.collect.store import connect, connect_readonly, digest, read_claims, record
from hanset_active.collect.targets import source_authorization
from hanset_twin.daemon import ProcessLock

FIXTURES = json.loads((Path(__file__).parent / 'fixtures/official_web_changes/transitions.json').read_text())
URL = 'https://venue.example/hours'
TARGET = {'venue_id': 'venue-1', 'venue_key': 'example-sauna', 'name': 'Example Sauna',
          'urls': [URL], 'domains': {'venue.example'}, 'tavily': None}


@pytest.fixture
def store(tmp_path):
    root = tmp_path / 'ig'
    root.mkdir()
    with ProcessLock(root / 'daemon.lock'):
        db = connect(root)
        changes.bootstrap_baselines(db, [TARGET])
        yield db, root
        db.close()


def observe(db, field='hours', value=None, day=1, target=None, claims_override=None, detect=True, url=None):
    target = target or TARGET
    value = FIXTURES[field]['before'] if value is None else value
    url = url or target['urls'][0]
    at = f'2026-11-{day:02d}T04:15:00+09:00'
    text_hash = digest(json.dumps([field, value], sort_keys=True))
    quote = value if isinstance(value, str) else json.dumps(value)
    record(db, kind='web', url=url, text_hash=text_hash, disposition='applied',
           claims=[{'field': field, 'value': value, 'quote': quote, 'rule_id': 'synthetic'}],
           venue_id=target['venue_id'], venue_key=target['venue_key'], seen_at=at)
    claims = [c for c in read_claims(db) if c['content_hash'] == text_hash and c['source_url'] == url]
    row = {**target, 'url': url, 'final_url': url, 'seen_at': at, 'content_hash': text_hash,
           'source_authorization': source_authorization(target, url)}
    oid = changes.insert_observation(db, row, claims if claims_override is None else claims_override)
    result = changes.process_pending(db, [target]) if detect else None
    return oid, result


def events(db):
    return [dict(r) for r in db.execute('SELECT * FROM web_change_events ORDER BY event_seq')]


@pytest.mark.parametrize('field', changes.FIELDS)
def test_seed_change_replay_and_three_transitions(store, field):
    db, _ = store
    before, after = FIXTURES[field]['before'], FIXTURES[field]['after']
    oid, _ = observe(db, field, before)
    assert events(db) == []
    observe(db, field, before)
    assert db.execute('SELECT count(*) FROM web_change_observations').fetchone()[0] == 1
    for day, value in [(2, after), (3, before), (4, after)]:
        observe(db, field, value, day)
    found = events(db)
    assert len(found) == 3
    assert len({e['change_id'] for e in found}) == 3
    assert all(e['event_kind'] == field and e['predecessor_observation_id'] for e in found)
    assert found[0]['predecessor_observation_id'] == oid
    observe(db, field, after, 4)
    assert len(events(db)) == 3
    # Repeated content reused two receipts, while four observations persisted.
    assert db.execute('SELECT count(*) FROM receipts').fetchone()[0] == 2
    assert db.execute('SELECT count(*) FROM web_change_observations').fetchone()[0] == 4
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE web_change_events SET review_reason='altered'")
    db.rollback()


@pytest.mark.parametrize('field,value', [
    ('hours', {'hours': '9:00 – 18:00', 'scope': 'weekday'}),
    ('price_day_pass', {**FIXTURES['price_day_pass']['before'], 'amount': '28,000원'}),
])
def test_formatting_noop(store, field, value):
    db, _ = store
    observe(db, field)
    observe(db, field, value, 2)
    assert events(db) == []


def test_qualifiers_missing_added_and_missing_field(store):
    db, _ = store
    observe(db)
    observe(db, value=FIXTURES['qualifier_change'], day=2)
    assert {e['event_kind'] for e in events(db)} == {'qualifier_added', 'qualifier_missing'}
    assert not any(e['usable_for_viral'] for e in events(db))
    baseline = [tuple(r) for r in db.execute('SELECT comparison_key FROM web_change_baselines')]
    observe(db, day=3, claims_override=[])
    assert [tuple(r) for r in db.execute('SELECT comparison_key FROM web_change_baselines')] == baseline
    assert len(events(db)) == 2


@pytest.mark.parametrize('qualifier,value', [('facility', 'pool'), ('scope', 'weekend')])
def test_hours_qualifier_scopes(store, qualifier, value):
    db, _ = store
    observe(db)
    observe(db, value={**FIXTURES['hours']['after'], qualifier: value}, day=2)
    assert not any(e['event_kind'] == 'hours' for e in events(db))


@pytest.mark.parametrize('qualifier,value', [('ticket','drop_in'),('audience','child'),('duration_hours',2)])
def test_price_qualifier_scopes(store, qualifier, value):
    db, _ = store
    observe(db, 'price_day_pass')
    observe(db, 'price_day_pass', {**FIXTURES['price_day_pass']['after'], qualifier: value}, 2)
    assert not any(e['event_kind'] == 'price_day_pass' for e in events(db))


@pytest.mark.parametrize('override', [
    {'source_kind':'instagram'}, {'source_type':'sns'}, {'evidence_origin':None},
    {'verbatim_source':0}, {'evidence_role':'summary_span'}, {'venue_id':'other'},
    {'source_url':'https://other.example/hours'},
])
def test_ineligible_claims_do_not_contribute(store, override):
    db, _ = store
    observe(db)
    claim = {**read_claims(db)[0], 'value': FIXTURES['hours']['after'], **override}
    observe(db, day=2, claims_override=[claim])
    assert events(db) == []


def test_leads_denied_and_authorization_change(store):
    db, _ = store
    observe(db)
    lead = {**TARGET, 'urls': []}
    assert source_authorization(lead, URL) is None
    for url in ('https://naver.com/hours', 'https://instagram.com/venue/', 'https://localhost/hours'):
        assert source_authorization({**TARGET, 'urls':[url]}, url) is None
    observe(db, value=FIXTURES['hours']['after'], day=2, detect=False)
    changes.process_pending(db, [])
    assert [e['event_kind'] for e in events(db)] == ['authorization_changed']
    assert changes.change_report(db)['baselines'] == 1


def test_detector_failure_is_pending_and_retry_exactly_once(store, monkeypatch):
    db, _ = store
    observe(db)
    original = changes._derive
    def fail(db, observation):
        original(db, observation)
        raise RuntimeError('injected')
    with monkeypatch.context() as patch:
        patch.setattr(changes, '_derive', fail)
        _, result = observe(db, value=FIXTURES['hours']['after'], day=2)
    assert result['reason'] == 'detector_error'
    assert len(read_claims(db)) == 2 and events(db) == []
    assert changes.change_report(db)['pending_observations'] == 1
    assert changes.process_pending(db, [TARGET])['processed'] == 1
    assert len(events(db)) == 1
    changes.process_pending(db, [TARGET])
    assert len(events(db)) == 1


def test_bootstrap_exact_unambiguous_provenance_and_rebaseline(store):
    db, _ = store
    observe(db, detect=False)
    db.execute('DELETE FROM web_change_metadata')
    db.commit()
    changes.bootstrap_baselines(db, [TARGET])
    row = db.execute('SELECT * FROM web_change_baselines').fetchone()
    assert row['value_kind'] == 'bootstrap_claim' and row['source_claim_id'] and row['observed_seq'] == 0
    changes.process_pending(db, [TARGET])
    observe(db, value=FIXTURES['hours']['after'], day=2)
    assert len(events(db)) == 1
    changes.put_meta(db, 'normalizer_version', 'future')
    db.commit()
    observe(db, day=3)
    assert changes.change_report(db)['rebaseline_required']
    assert changes.change_report(db)['pending_observations'] == 1
    changes.rebaseline(db, [TARGET])
    assert not changes.needs_rebaseline(db)
    assert changes.metadata(db)['rebaseline_generation'] == '1'
    assert len(events(db)) == 1
    assert changes.change_report(db)['pending_observations'] == 0


def test_ambiguous_bootstrap_waits_for_observation(store):
    db, _ = store
    observe(db, detect=False)
    observe(db, value=FIXTURES['hours']['after'], day=2, detect=False)
    db.execute("DELETE FROM web_change_metadata WHERE key='bootstrap_version'")
    db.commit()
    changes.bootstrap_baselines(db, [TARGET])
    assert changes.metadata(db)['bootstrap_ambiguous'] == '1'
    assert changes.change_report(db)['baselines'] == 0
    assert events(db) == []
    changes.process_pending(db, [TARGET])
    assert len(events(db)) == 1


def test_missing_db_and_old_schema_are_readonly(tmp_path):
    settings = CollectSettings(data_dir=tmp_path/'missing', active_dir=tmp_path/'active', twin_dir=tmp_path/'twin')
    assert run_report(settings)['changes']['events'] == 0
    assert run_detect_changes(settings, dry_run=True)['events'] == 0
    assert run_export_changes(settings)['reason'] == 'no_events'
    assert not settings.data_dir.exists()
    settings.data_dir.mkdir()
    path = settings.data_dir/'collector.sqlite3'
    legacy = sqlite3.connect(path)
    # A minimal receipt-only historical schema also exercises report guards.
    legacy.execute('CREATE TABLE legacy_marker (value TEXT)')
    legacy.execute("INSERT INTO legacy_marker VALUES ('preserved')")
    legacy.commit()
    legacy.close()
    original = path.read_bytes()
    assert run_report(settings)['changes']['events'] == 0
    assert run_detect_changes(settings, dry_run=True, rebaseline=True)['events'] == 0
    assert path.read_bytes() == original
    assert sorted(p.name for p in settings.data_dir.iterdir()) == ['collector.sqlite3']
    with ProcessLock(settings.data_dir/'daemon.lock'):
        db = connect(settings.data_dir)
        assert db.execute('SELECT value FROM legacy_marker').fetchone()[0] == 'preserved'
        assert events(db) == []
        db.close()


def test_bootstrap_predecessor_claim_is_preserved(store):
    db, _ = store
    from hanset_active.collect.store import record, digest
    value = FIXTURES['hours']['before']
    record(db, kind='web', url=URL, text_hash=digest('historical'), disposition='applied',
           claims=[{'field':'hours','value':value,'quote':'Weekday hours 09:00-18:00','rule_id':'synthetic'}],
           venue_id=TARGET['venue_id'],venue_key=TARGET['venue_key'],seen_at='2026-10-01T04:15:00+09:00')
    db.execute("DELETE FROM web_change_metadata WHERE key='bootstrap_version'")
    db.commit()
    changes.bootstrap_baselines(db, [TARGET])
    claim_id = read_claims(db)[0]['claim_id']
    observe(db, value=FIXTURES['hours']['after'], day=2)
    assert events(db)[0]['predecessor_claim_id'] == claim_id
    assert events(db)[0]['predecessor_observation_id'] is None


def test_invalid_targets_retry_bootstrap_and_dry_run_snapshot(store):
    db, root = store
    db.execute("DELETE FROM web_change_metadata WHERE key='bootstrap_version'")
    db.commit()
    changes.bootstrap_baselines(db, None)
    assert 'bootstrap_version' not in changes.metadata(db)
    observe(db)
    settings = CollectSettings(data_dir=root, active_dir=root.parent/'active', twin_dir=root.parent/'twin')
    before = {p.name:p.read_bytes() for p in root.iterdir() if p.is_file()}
    assert run_detect_changes(settings, dry_run=True, rebaseline=True)['observations'] == 1
    after = {p.name:p.read_bytes() for p in root.iterdir() if p.is_file()}
    assert before == after


def test_different_venues_never_compare(store):
    db, _ = store
    observe(db)
    other = {**TARGET,'venue_id':'venue-2','venue_key':'other-sauna',
             'urls':['https://other.example/hours'],'domains':{'other.example'}}
    observe(db, value=FIXTURES['hours']['after'], day=2, target=other, detect=False)
    changes.process_pending(db, [TARGET, other])
    assert events(db) == []
    assert changes.change_report(db)['baselines'] == 2


def test_authorization_change_never_compares_old_target(store):
    db, _ = store
    observe(db)
    changed = {**TARGET, 'urls':[URL, 'https://venue.example/prices']}
    observe(db, value=FIXTURES['hours']['after'], day=2, target=changed)
    assert [e['event_kind'] for e in events(db)] == ['authorization_changed']
    assert not changes.needs_rebaseline(db)


def test_readonly_snapshot_includes_commits_and_ignores_uncommitted_wal(store):
    db, root = store
    observe(db)
    db.execute("INSERT INTO web_change_metadata VALUES ('uncommitted','hidden')")
    before = {p.name:p.read_bytes() for p in root.iterdir() if p.is_file()}
    ro = connect_readonly(root)
    assert changes.change_report(ro)['observations'] == 1
    assert 'uncommitted' not in changes.metadata(ro)
    ro.close()
    assert before == {p.name:p.read_bytes() for p in root.iterdir() if p.is_file()}
    db.rollback()
    # Exercise WAL reset with leftover frames from the previous generation.
    db.execute('PRAGMA wal_checkpoint(RESTART)')
    changes.put_meta(db, 'committed', 'visible')
    db.commit()
    ro = connect_readonly(root)
    assert changes.metadata(ro)['committed'] == 'visible'
    assert changes.change_report(ro)['observations'] == 1
    ro.close()
