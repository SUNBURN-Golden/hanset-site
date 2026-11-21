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


def first_party(target):
    return {**target, 'official_urls': target['urls'], 'scope_domains': target['domains']}


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
    assert result['targets'] == [first_party(target)]
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


def seed_rows(db, target=None, *, seed_id='frozen', domain='seed.example.test', name=None,
              website=None, approved=True):
    name = name or target['name']
    norm = normalize_name(name)
    with db:
        db.execute("INSERT OR REPLACE INTO run_checkpoints(checkpoint_id,cursor) VALUES ('seed_freeze','frozen')")
        if approved:
            db.execute('INSERT OR IGNORE INTO seed_domains(seed_domain_id,seed_id,domain) VALUES (?,?,?)',
                       ('domain-' + seed_id + domain, seed_id, domain))
        db.execute('INSERT INTO seed_venues(seed_venue_id,seed_id,name,normalized_name,domain,website) VALUES (?,?,?,?,?,?)',
                   ('venue-' + seed_id + domain + norm, seed_id, name, norm, domain, website))


def test_frozen_seed_requires_unique_exact_name_and_domain_tie(env):
    _, _, db = env
    target = eligible(db, 1)
    seed_rows(db, target)
    seed_rows(db, target, seed_id='obsolete', domain='obsolete.example.test')
    seed_rows(db, domain='unrelated.example.test', name='Box B')
    with db:
        db.execute("UPDATE venues SET seed_match='frozen_seed'")
    assert [(t['name'], t['urls']) for t in assembly(env)['targets']] == [
        (target['name'], ['https://seed.example.test']), ('Box B', ['https://unrelated.example.test']),
    ]
    other = eligible(db, 2)
    with db:
        db.execute('UPDATE venues SET name=? WHERE venue_id=?', (target['name'], other['venue_id']))
    assert [t['name'] for t in assembly(env)['targets']] == ['Box B']


def test_seed_alias_tie_and_missing_domain_or_marker_do_not_attach(env):
    _, _, db = env
    target = eligible(db, 1)
    with db:
        db.execute('INSERT INTO venue_aliases(alias_id,venue_id,alias,normalized_name) VALUES (?,?,?,?)',
                   ('alias', target['venue_id'], 'Old Fixture', normalize_name('Old Fixture')))
    seed_rows(db, target, name='Old Fixture')
    assert assembly(env)['targets'][0]['urls'] == ['https://seed.example.test']
    with db:
        db.execute('DELETE FROM seed_domains')
    assert assembly(env)['targets'] == []
    with db:
        db.execute("INSERT INTO seed_domains(seed_domain_id,seed_id,domain) VALUES ('sd','frozen','seed.example.test')")
        db.execute('DELETE FROM run_checkpoints')
    assert assembly(env)['targets'] == []


def test_seed_only_targets_are_stable_web_only_and_read_only(env):
    settings, clock, db = env
    domain = 'spa.example.test'
    url = f'https://{domain}/official/hours'
    for freeze in ('old', 'frozen'):
        seed_rows(db, name='Seed Spa A', domain=domain, website=url, seed_id=freeze, approved=False)
    seed_rows(db, name='Box B', domain='box.example.test')
    before = list(db.iterdump())
    result = assembly(env)
    by_name = {t['name']: t for t in result['targets']}
    assert set(by_name) == {'Seed Spa A', 'Box B'}
    assert by_name['Seed Spa A']['urls'] == [url]
    assert by_name['Box B']['urls'] == ['https://box.example.test']
    assert all(t['venue_id'].startswith('seed:') and t['tavily'] is None for t in by_name.values())
    assert {tuple(r['channels']) for r in result['snapshot'][0]} == {('web',)}
    outcome = run_plan(settings, clock=clock, ranking='local', testing=True)
    plan = json.loads((settings.data_dir / 'plans' / (outcome['day'] + '.json')).read_text())
    assert validate_plan(plan, outcome['day']) and len(plan['web_targets']) == 2
    assert plan['ig_read_list'] == []
    assert list(db.iterdump()) == before
    with db:
        db.execute("UPDATE run_checkpoints SET cursor='old' WHERE checkpoint_id='seed_freeze'")
    assert assembly(env)['targets'] == [by_name['Seed Spa A']]


@pytest.mark.parametrize('name,domain,website,approved', [
    ('Seed Spa A', 'instagram.com', 'https://instagram.com/venue', True),
    ('Seed Spa A 추천', 'spa.example.test', None, True),
    ('Seed Spa A', 'spa.example.test', 'http://spa.example.test/venue', True),
    ('Seed Spa A', 'spa.example.test', None, False),
    ('Seed Spa A', 'spa.example.test', 'https://spa.example.test/?sid=fixture', True),
    ('Seed Spa A', 'spa.example.test', 'https://localhost/venue', True),
    ('Seed Spa A', 'spa.example.test', 'https://spa.example.test:invalid/', True),
])
def test_seed_url_guards_also_apply_to_gap_augmentation(env, name, domain, website, approved):
    _, _, db = env
    seed_rows(db, name=name, domain=domain, website=website, approved=approved)
    assert assembly(env)['targets'] == []
    target = eligible(db, 1)
    with db:
        db.execute('UPDATE venues SET name=? WHERE venue_id=?', (name, target['venue_id']))
    assert assembly(env)['targets'] == []


def test_seed_same_host_is_not_identity_and_closed_match_is_skipped(env):
    _, _, db = env
    gap_target = eligible(db, 1, website='https://spa.example.test/gap')
    closed = venue(db, 2, state='closed')
    seed_rows(db, name='Seed Spa A', domain='spa.example.test', website='https://spa.example.test/seed')
    seed_rows(db, closed, domain='closed.example.test')
    gap_result, seed_result = assembly(env)['targets']
    assert gap_result['venue_id'] == gap_target['venue_id'] and gap_result['urls'] == ['https://spa.example.test/gap']
    assert seed_result['name'] == 'Seed Spa A' and seed_result['venue_id'].startswith('seed:')


@pytest.mark.parametrize('matched', [False, True])
def test_manual_or_disabled_seed_selector_never_extends_authorization(env, matched):
    settings, _, db = env
    seed_rows(db, name='Seed Spa A', domain='spa.example.test')
    if matched:
        target = eligible(db, 1)
        with db:
            db.execute("UPDATE venues SET name='Seed Spa A'")
    else:
        target = {'venue_id': 'operator-id', 'venue_key': 'operator-key'}
    manual = {**target, 'name': 'Seed Spa A', 'urls': [], 'domains': {'spa.example.test'},
              'tavily': {'venue_id': target['venue_id'], 'field': 'hours'}}
    path = settings.data_dir / 'web_targets.json'
    save_targets(path, [manual])
    assert assembly(env)['targets'] == [first_party(manual)]
    path.write_text(json.dumps({'schema_version': 1, 'targets': [
        {'enabled': False, 'venue_id': 'old-id', 'venue_key': 'Seed Spa A'},
    ]}))
    assert assembly(env)['targets'] == []


def test_gap_then_seed_fallbacks_fill_target_cap_with_all_urls(env):
    _, _, db = env
    gap_target = eligible(db, 1, website='https://gap.example.test/hours')
    # A seed-backed existing identity with no gaps keeps every distinct URL.
    existing = venue(db, 2)
    for n in range(5):
        seed_rows(db, existing, domain=f'branch{n}.example.test',
                  website=f'https://branch{n}.example.test/location{n}')
    result = assembly(env)['targets']
    assert [t['venue_id'] for t in result] == [gap_target['venue_id'], existing['venue_id']]
    assert len(result[1]['urls']) == 5 and len(set(result[1]['urls'])) == 5
    for n in range(MAX_TARGETS):
        seed_rows(db, name=f'Seed Spa A {n}', domain=f'box{n}.example.test',
                  website=f'https://box{n}.example.test/location{n}')
    result = assembly(env)
    assert len(result['targets']) == MAX_TARGETS
    assert result['targets'][0]['venue_id'] == gap_target['venue_id']
    assert codes(result)['derived_truncated'] >= 1


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
    assert parse_targets(path, testing=True) == []


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
    assert result['db_status'] == 'unreadable' and result['targets'] == [first_party(manual)]
    assert codes(result)['active_db_unreadable'] == 1
    assert result['snapshot'][0][0]['identity_status'] == 'unknown'
    assert _change_targets(settings) is None
    # Ad-hoc authorization never consults the failed active DB.
    assert _change_targets(settings, settings.data_dir / 'web_targets.json') == [first_party(manual)]


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
