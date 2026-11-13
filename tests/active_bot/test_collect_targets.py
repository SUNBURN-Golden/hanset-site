"""Offline ownership, suppression, cap and read-only assembly regressions."""
import json
import sqlite3

import pytest

from hanset_active.collect import CollectError
from hanset_active.collect import plan as planning
from hanset_active.collect.config import MAX_TARGETS
from hanset_active.collect.plan import assemble_targets, local_target_order, run_plan, validate_plan
from hanset_active.collect.service import _change_targets
from hanset_active.collect.targets import parse_targets, source_authorization, target_fingerprint
from hanset_active.util.timeutil import kst_now
from hanset_active.venues import normalize_name
from tests.active_bot.test_collect_plan import env, fact, gap, save_targets, venue


def eligible(db, n, *, website=None, domains=(), kind='sauna', state='active', field='hours'):
    target = venue(db, n, kind, state)
    with db:
        db.execute('UPDATE venues SET website=?,domains=? WHERE venue_id=?',
                   (website, json.dumps(domains), target['venue_id']))
    gap(db, target, field=field)
    return target


def assembly(env, **kwargs):
    settings, clock, _ = env
    return assemble_targets(settings, now=kst_now(clock), testing=True, **kwargs)


def codes(result):
    return result['snapshot'][2]


def test_derived_candidates_and_plan_are_read_only(env, monkeypatch):
    settings, clock, db = env
    target = eligible(db, 1, website='https://venue1.example/hours')
    before = list(db.iterdump())
    # Prohibit the migrating connection and all lifecycle writes in planning.
    def forbidden(*args, **kwargs):
        raise AssertionError('planning attempted a write path')
    monkeypatch.setattr('hanset_active.db.connect', forbidden)
    monkeypatch.setattr('hanset_active.gaps.ensure_gaps', forbidden)
    monkeypatch.setattr('hanset_active.gaps.resolve', forbidden)
    monkeypatch.setattr('hanset_active.gaps.reopen', forbidden)
    result = assembly(env)
    assert result['targets_status'] == 'missing' and result['db_status'] == 'ok'
    assert result['targets'] == [target]
    assert result['snapshot'][0][0]['channels'] == ['web']
    outcome = run_plan(settings, clock=clock, ranking='local', testing=True)
    plan = json.loads((settings.data_dir / 'plans' / (outcome['day'] + '.json')).read_text())
    assert plan['ranking']['candidate_count'] == 1 and validate_plan(plan, outcome['day'])
    assert list(db.iterdump()) == before
    assert not (settings.data_dir / 'web_targets.json').exists()


@pytest.mark.parametrize('website', [
    'https://naver.com/venue', 'https://instagram.com/venue', 'https://runport.example/',
    'https://127.0.0.1/', 'https://localhost/', 'https://' + 'user:pass' + '@' + 'venue.example/',
    'https://venue.example/?api_key=synthetic', 'https://venue.example/?contact=fixture%40example.test',
])
def test_unsafe_or_denied_websites_are_excluded(env, website):
    _, _, db = env
    eligible(db, 1, website=website)
    result = assembly(env)
    assert result['targets'] == [] and codes(result)['derived_domain_denied'] == 1


def test_portals_closed_nonapplicable_and_stale_satisfied_gaps_excluded(env, monkeypatch):
    _, clock, db = env
    monkeypatch.setattr('hanset_active.util.followups.portal_domains', lambda: {'directory.example'})
    eligible(db, 1, website='https://directory.example/venue')
    eligible(db, 2, website='https://closed.example/', state='closed')
    eligible(db, 3, website='https://fitness.example/', kind='crossfit_box', field='sauna_temp_c')
    fresh = eligible(db, 4, website='https://fresh.example/')
    fact(db, fresh, now=kst_now(clock))
    eligible(db, 5, website='https://unknown.example/', kind='unrecognized')
    assert assembly(env)['targets'] == []


def test_http_only_is_not_rewritten_but_independent_domain_can_be_used(env):
    _, _, db = env
    eligible(db, 1, website='http://venue1.example/sauna/hours')
    assert assembly(env)['targets'] == []
    assert codes(assembly(env))['derived_http_only'] == 1
    with db:
        db.execute('UPDATE venues SET domains=?', (json.dumps(['venue1.example']),))
    result = assembly(env)
    assert result['targets'][0]['urls'] == ['https://venue1.example']
    assert '/sauna/hours' not in result['targets'][0]['urls'][0]


def seed_rows(db, target, *, seed_id='frozen', domain='seed.example', name=None):
    name = name or target['name']
    with db:
        db.execute("INSERT OR REPLACE INTO run_checkpoints(checkpoint_id,cursor) VALUES ('seed_freeze','frozen')")
        db.execute('INSERT INTO seed_domains(seed_domain_id,seed_id,domain) VALUES (?,?,?)',
                   ('domain-' + seed_id + domain, seed_id, domain))
        db.execute('INSERT INTO seed_venues(seed_venue_id,seed_id,name,normalized_name,domain) VALUES (?,?,?,?,?)',
                   ('venue-' + seed_id + domain, seed_id, name, normalize_name(name), domain))


def test_frozen_seed_requires_unique_exact_name_and_domain_tie(env):
    _, _, db = env
    target = eligible(db, 1)
    seed_rows(db, target)
    seed_rows(db, target, seed_id='obsolete', domain='obsolete.example')
    seed_rows(db, target, domain='unrelated.example', name='Unrelated Venue')
    with db:
        db.execute("UPDATE venues SET seed_match='frozen_seed'")
    assert assembly(env)['targets'][0]['urls'] == ['https://seed.example']
    other = eligible(db, 2)
    with db:
        db.execute('UPDATE venues SET name=? WHERE venue_id=?', (target['name'], other['venue_id']))
    assert assembly(env)['targets'] == []


def test_seed_alias_tie_and_missing_domain_or_marker_do_not_attach(env):
    _, _, db = env
    target = eligible(db, 1)
    with db:
        db.execute('INSERT INTO venue_aliases(alias_id,venue_id,alias,normalized_name) VALUES (?,?,?,?)',
                   ('alias', target['venue_id'], 'Old Fixture', normalize_name('Old Fixture')))
    seed_rows(db, target, name='Old Fixture')
    assert assembly(env)['targets'][0]['urls'] == ['https://seed.example']
    with db:
        db.execute('DELETE FROM seed_domains')
    assert assembly(env)['targets'] == []
    with db:
        db.execute("INSERT INTO seed_domains(seed_domain_id,seed_id,domain) VALUES ('sd','frozen','seed.example')")
        db.execute('DELETE FROM run_checkpoints')
    assert assembly(env)['targets'] == []


@pytest.mark.parametrize('signal', ['alias', 'name', 'domain'])
def test_manual_identity_resolution_is_in_memory(env, signal):
    settings, _, db = env
    target = eligible(db, 1, website='https://venue1.example/hours')
    manual = {**target, 'venue_id': 'old-id', 'venue_key': 'old-key', 'name': 'Old Name'}
    if signal == 'alias':
        with db:
            db.execute('INSERT INTO venue_aliases(alias_id,venue_id,normalized_name) VALUES (?,?,?)',
                       ('alias', target['venue_id'], normalize_name(manual['name'])))
        manual.update(urls=['https://manual.example/hours'], domains={'manual.example'})
    elif signal == 'name':
        manual.update(name=target['name'], urls=['https://manual.example/hours'], domains={'manual.example'})
    path = settings.data_dir / 'web_targets.json'
    save_targets(path, [manual])
    before = path.read_bytes(), list(db.iterdump())
    result = assembly(env)
    assert result['targets'][0]['venue_id'] == target['venue_id']
    assert result['targets'][0]['name'] == manual['name']
    assert result['targets'][0]['domains'] == manual['domains']
    assert before == (path.read_bytes(), list(db.iterdump()))
    assert _change_targets(settings)[0]['venue_id'] == target['venue_id']


@pytest.mark.parametrize('case', ['unmatched', 'shared_domain', 'conflicting_alias_domain'])
def test_unknown_manual_identity_is_visible_with_fixed_diagnostic(env, case):
    settings, clock, db = env
    target = venue(db, 1)
    other = venue(db, 2)
    manual = {**target, 'venue_id': 'missing', 'venue_key': 'missing', 'name': 'Missing'}
    if case != 'unmatched':
        with db:
            db.execute('UPDATE venues SET website=? WHERE venue_id=?', (target['urls'][0], target['venue_id']))
            if case == 'shared_domain':
                db.execute('UPDATE venues SET website=? WHERE venue_id=?', (target['urls'][0], other['venue_id']))
            else:
                db.execute('INSERT INTO venue_aliases(alias_id,venue_id,normalized_name) VALUES (?,?,?)',
                           ('alias', other['venue_id'], normalize_name(manual['name'])))
    save_targets(settings.data_dir / 'web_targets.json', [manual])
    result = assembly(env)
    expected = 'manual_identity_unmatched' if case == 'unmatched' else 'manual_identity_ambiguous'
    assert codes(result)[expected] == 1
    assert result['targets'][0]['venue_id'] == 'missing'
    assert result['snapshot'][0][0]['reason'] == 'identity_unknown'
    outcome = run_plan(settings, clock=clock, testing=True)
    plan = json.loads((settings.data_dir / 'plans' / (outcome['day'] + '.json')).read_text())
    assert validate_plan(plan, outcome['day']) and plan['ranked_venues'][0]['gap_count'] is None


def test_manual_derived_dedupe_and_authorization_not_widened(env):
    settings, _, db = env
    target = eligible(db, 1, website='https://venue1.example/hours/',
                      domains=['venue1.example', 'extra.example'])
    target['tavily'] = {'venue_id': target['venue_id'], 'field': 'hours'}
    save_targets(settings.data_dir / 'web_targets.json', [target, target])
    result = assembly(env)
    assert len(result['targets']) == 1
    merged = result['targets'][0]
    assert merged['urls'] == [target['urls'][0], 'https://venue1.example']
    assert merged['domains'] == target['domains'] and merged['tavily'] == target['tavily']
    assert codes(result)['derived_domain_denied'] == 1
    for url in merged['urls']:
        assert source_authorization(merged, url)['target_fingerprint'] == target_fingerprint(merged)
    assert target_fingerprint(_change_targets(settings)[0]) == target_fingerprint(merged)


def test_disabled_selectors_are_lenient_resolved_and_enabled_wins(env):
    settings, _, db = env
    target = eligible(db, 1, website='https://venue1.example/hours')
    path = settings.data_dir / 'web_targets.json'
    rows = [{'enabled': False}, {'enabled': False, 'venue_id': [], 'venue_key': None},
            {'enabled': False, 'venue_id': 'old', 'venue_key': target['name']}]
    path.write_text(json.dumps({'schema_version': 1, 'targets': rows}))
    assert parse_targets(path, testing=True) == []
    result = assembly(env)
    assert result['targets_status'] == 'empty' and result['targets'] == []
    assert codes(result)['derived_suppressed'] == 1
    rows.append({**{k: target[k] for k in ('venue_id', 'venue_key', 'name')},
                 'official_urls': target['urls'], 'official_domains': list(target['domains'])})
    path.write_text(json.dumps({'schema_version': 1, 'targets': rows}))
    assert assembly(env)['targets'][0]['venue_id'] == target['venue_id']


def test_derived_cap_priority_and_manual_at_cap_reports_truncation(env):
    settings, _, db = env
    for n in range(MAX_TARGETS + 3):
        eligible(db, n, website=f'https://venue{n}.example/hours')
    gap(db, {'venue_id': 'v102'}, field='price_day_pass', state='conflict')
    result = assembly(env)
    assert len(result['targets']) == MAX_TARGETS and codes(result)['derived_truncated'] == 3
    # With equal projected needs the deterministic identity is the tie breaker.
    assert [t['venue_id'] for t in result['targets']] == [f'v{n:03}' for n in range(MAX_TARGETS)]
    manual = [{**result['targets'][0], 'venue_id': f'm{n:03}', 'venue_key': f'manual{n:03}',
               'name': f'Manual {n}', 'urls': [f'https://manual{n}.example/hours'],
               'domains': {f'manual{n}.example'}} for n in range(MAX_TARGETS)]
    save_targets(settings.data_dir / 'web_targets.json', manual)
    result = assembly(env)
    assert len(result['targets']) == MAX_TARGETS and codes(result)['derived_truncated'] == MAX_TARGETS + 3
    assert {t['venue_id'] for t in result['targets']} == {t['venue_id'] for t in manual}
    rows = [{'enabled': False}] * (MAX_TARGETS + 1)
    path = settings.data_dir / 'web_targets.json'
    path.write_text(json.dumps({'schema_version': 1, 'targets': rows}))
    with pytest.raises(CollectError, match='targets_invalid'):
        parse_targets(path, testing=True)


@pytest.mark.parametrize('status', ['missing', 'empty', 'invalid'])
def test_manual_file_status_controls_derivation(env, status):
    settings, _, db = env
    eligible(db, 1, website='https://venue1.example/hours')
    path = settings.data_dir / 'web_targets.json'
    if status == 'empty':
        save_targets(path, [])
    elif status == 'invalid':
        path.write_text('invalid')
    result = assembly(env)
    assert result['targets_status'] == status
    assert bool(result['targets']) == (status != 'invalid')
    assert (_change_targets(settings) is None) == (status == 'invalid')


def test_snapshot_reuse_and_mid_snapshot_failure_discards_partial_derivation(env, monkeypatch):
    settings, clock, db = env
    target = eligible(db, 1, website='https://venue1.example/hours')
    manual = {**target, 'venue_id': 'unmatched'}
    save_targets(settings.data_dir / 'web_targets.json', [manual])
    result = assembly(env)
    def forbidden(*args, **kwargs):
        raise AssertionError('snapshot reread')
    monkeypatch.setattr(planning, 'snapshot', forbidden)
    assert local_target_order(settings, result['targets'], now=kst_now(clock),
                              materialized=result['snapshot']) == result['targets']
    original = planning._snapshot_rows
    def failed(db, *args, **kwargs):
        if db is not None:
            raise sqlite3.OperationalError('synthetic snapshot failure')
        return original(db, *args, **kwargs)
    monkeypatch.setattr(planning, '_snapshot_rows', failed)
    result = assembly(env)
    assert result['db_status'] == 'unreadable' and result['targets'] == [manual]
    assert codes(result)['active_db_unreadable'] == 1
    assert result['snapshot'][0][0]['identity_status'] == 'unknown'
    assert _change_targets(settings) is None
    # Ad-hoc authorization never consults the failed active DB.
    assert _change_targets(settings, settings.data_dir / 'web_targets.json') == [manual]


def test_ownership_fingerprint_changes_and_no_venues_is_a_noop(env):
    _, _, db = env
    assert assembly(env)['targets'] == []
    target = eligible(db, 1, website='https://venue1.example/hours')
    initial = assembly(env)['snapshot'][1]
    with db:
        db.execute('UPDATE venues SET website=? WHERE venue_id=?', ('https://venue1.example/price', target['venue_id']))
    assert initial != assembly(env)['snapshot'][1]
    before_alias = assembly(env)['snapshot'][1]
    with db:
        db.execute('INSERT INTO venue_aliases(alias_id,venue_id,normalized_name) VALUES (?,?,?)',
                   ('alias', target['venue_id'], 'alias'))
    assert before_alias != assembly(env)['snapshot'][1]


def test_derivation_orders_by_projected_gap_count_and_preserves_manual_urls(env):
    settings, clock, db = env
    less = eligible(db, 1, website='https://venue1.example/hours')
    gap(db, less, field='price_day_pass', state='conflict')
    fact(db, less, now=kst_now(clock))
    more = eligible(db, 2, website='https://venue2.example/hours')
    result = assembly(env)
    assert [t['venue_id'] for t in result['targets']] == [more['venue_id'], less['venue_id']]
    assert [r['gap_count'] for r in result['snapshot'][0]] == [6, 5]
    extra = {**less, 'urls': ['https://manual.example/sauna'], 'domains': {'manual.example'}}
    save_targets(settings.data_dir / 'web_targets.json', [less, extra])
    merged = next(t for t in assembly(env)['targets'] if t['venue_id'] == less['venue_id'])
    assert merged['urls'][:2] == less['urls'] + extra['urls']
    assert merged['domains'] == less['domains'] | extra['domains']
