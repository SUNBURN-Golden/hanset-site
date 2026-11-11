"""Offline daily ordering, evidence authority, replay and planner boundaries."""
import copy
import json
import sqlite3
from datetime import datetime, timedelta

import pytest
import requests

from hanset_active.collect import CollectError
from hanset_active.collect.config import CollectSettings, MODEL_CALL_CAP
from hanset_active.collect.deepseek import OUTPUT_TOKENS, QWEN_ENDPOINT
from hanset_active.collect.plan import (
    DeepSeekRanker, _write, digest, ensure_daily_plan,
    local_target_order, order_targets, run_plan, snapshot, target_id, validate_plan,
)
from hanset_active.collect.service import run_web
from hanset_active.collect.state import State
from hanset_active.db import connect
from hanset_active.gaps import COUNTED_GAP_STATES, project_needs
from hanset_active.util.timeutil import FakeClock, KST, kst_now
from hanset_twin.daemon import ProcessLock
from tests.active_bot.test_collect_deepseek import KEY, QWEN_KEY, Response, Session, _envelope, _qwen_key_file


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.delenv('TAVILY_API_KEY', raising=False)


@pytest.fixture
def env(tmp_path):
    settings = CollectSettings(data_dir=tmp_path / 'ig', active_dir=tmp_path / 'active', twin_dir=tmp_path / 'twin')
    settings.data_dir.mkdir()
    clock = FakeClock(datetime(2026, 11, 10, 4, 15, tzinfo=KST))
    db = connect(settings.active_dir / 'active.sqlite3')
    yield settings, clock, db
    db.close()


def venue(db, n, kind='sauna', state='active'):
    entry = {'venue_id': f'v{n:03}', 'venue_key': f'key{n:03}', 'name': f'Fixture {n}',
             'urls': [f'https://venue{n}.example/hours'], 'domains': {f'venue{n}.example'}, 'tavily': None}
    with db:
        db.execute('INSERT INTO venues(venue_id,venue_key,name,type,state) VALUES (?,?,?,?,?)',
                   (entry['venue_id'], entry['venue_key'], entry['name'], kind, state))
    return entry


def fact(db, target, field='hours', *, now, label='fact', source=None, value='value',
         source_type='own_site', current=True, obs=None, valid_from=None):
    obs = obs or digest([target['venue_id'], field, value, source, label])
    source = source or target['urls'][0]
    with db:
        db.execute('INSERT INTO observations(obs_id,venue_key,field,value_hash,label_status,source_url,source_type,observed_at) VALUES (?,?,?,?,?,?,?,?)',
                   (obs, target['venue_key'], field, value, label, source, source_type, now.isoformat()))
        if current:
            db.execute('INSERT INTO venue_fields(vf_id,venue_id,field,value_hash,label_status,obs_id,valid_from) VALUES (?,?,?,?,?,?,?)',
                       (obs, target['venue_id'], field, value, label, obs, valid_from if valid_from is not None else now.isoformat()))
    return obs


def gap(db, target, field='hours', state='open', obs=None, next_at=None):
    with db:
        db.execute('INSERT OR REPLACE INTO gaps(gap_id,venue_id,field,state,obs_id,next_at) VALUES (?,?,?,?,?,?)',
                   (digest([target['venue_id'], field]), target['venue_id'], field, state, obs, next_at))


def watch(settings, targets):
    entries = [{'enabled': True, **{k: t[k] for k in ('venue_id', 'venue_key', 'name')},
                'handle': 'fixture.' + t['venue_key']} for t in targets]
    path = settings.data_dir / 'watchlist.json'
    path.write_text(json.dumps({'schema_version': 1, 'venues': entries}))
    return entries


def save_targets(path, targets):
    path.write_text(json.dumps({'schema_version': 1, 'targets': [
        {**{k: t[k] for k in ('venue_id', 'venue_key', 'name')},
         'official_urls': t['urls'], 'official_domains': sorted(t['domains']), 'tavily': t['tavily']}
        for t in targets]}))


def make_plan(env, targets, **kwargs):
    settings, clock, _ = env
    return ensure_daily_plan(settings, State(settings.data_dir, clock), targets, 'ok' if targets else 'empty',
                             clock=clock, ranking='local', **kwargs)


def reason_for(projected, field='hours'):
    return next((f['reason'] for f in projected['fields'] if f['field_id'] == field), None)


def test_applicability_and_more_gaps_first(env):
    settings, clock, db = env
    less, more = venue(db, 1), venue(db, 2)
    fact(db, less, now=kst_now(clock))
    targets = [less, more]
    watch(settings, targets)
    plan = make_plan(env, targets)
    assert [r['gap_count'] for r in plan['ranked_venues']] == [6, 5]
    assert [r['venue_id'] for r in plan['ranked_venues']] == ['v002', 'v001']
    assert plan['web_targets'][0]['venue_id'] == 'v002'
    assert plan['ig_read_list'][0]['venue_id'] == 'v002'
    assert validate_plan(plan, plan['day'])


@pytest.mark.parametrize('kind,count', [('sauna', 6), ('hotel_spa', 6), ('crossfit_box', 3), ('hyrox_box', 3)])
def test_profile_applicability(env, kind, count):
    _, clock, db = env
    target = venue(db, 1, kind)
    result = project_needs(db, target['venue_id'], now=kst_now(clock))
    assert result['gap_count'] == count
    if count == 3:
        assert {f['field_id'] for f in result['fields']} == {'hours', 'price_day_pass', 'foreigner_policy'}


@pytest.mark.parametrize('state', sorted(COUNTED_GAP_STATES))
def test_every_unresolved_gap_state_counts(env, state):
    _, clock, db = env
    target = venue(db, 1)
    gap(db, target, state=state)
    assert reason_for(project_needs(db, target['venue_id'], now=kst_now(clock))) == 'gap_' + state


@pytest.mark.parametrize('gap_state', [None, 'filled', 'open', 'conflict'])
def test_current_fact_beats_missing_or_stale_gap(env, gap_state):
    _, clock, db = env
    target = venue(db, 1)
    obs = fact(db, target, now=kst_now(clock))
    if gap_state:
        gap(db, target, state=gap_state, obs=obs)
    result = project_needs(db, target['venue_id'], now=kst_now(clock))
    assert result['gap_count'] == 5 and reason_for(result) is None
    assert bool(result['diagnostics']) == (gap_state in ('open', 'conflict'))


@pytest.mark.parametrize('case,reason', [
    ('missing_obs', 'evidence_invalid'), ('non_fact', 'evidence_invalid'),
    ('mismatch', 'evidence_invalid'), ('stale', 'refresh_due'), ('next_due', 'refresh_due'),
    ('no_current', 'evidence_invalid'), ('bad_valid_from', 'evidence_invalid'),
    ('conflict', 'evidence_conflict'), ('unknown_gap', 'gap_unknown'),
])
def test_evidence_validation(env, case, reason):
    _, clock, db = env
    target = venue(db, 1)
    now = kst_now(clock)
    if case == 'missing_obs':
        gap(db, target, state='filled')
    elif case == 'non_fact':
        obs = fact(db, target, now=now, label='source_claim')
        gap(db, target, state='filled', obs=obs)
    elif case == 'mismatch':
        other = venue(db, 2)
        obs = fact(db, other, now=now)
        gap(db, target, state='filled', obs=obs)
    elif case == 'unknown_gap':
        gap(db, target, state='unrecognized')
    else:
        stamp = now - timedelta(days=3) if case == 'stale' else now
        obs = fact(db, target, now=stamp, current=case != 'no_current',
                   valid_from='invalid' if case == 'bad_valid_from' else None)
        if case == 'next_due':
            gap(db, target, state='filled', obs=obs, next_at=now.isoformat())
        if case == 'conflict':
            fact(db, target, now=now, source='https://other.example/', value='different')
    assert reason_for(project_needs(db, target['venue_id'], now=now)) == reason


@pytest.mark.parametrize('source_type', ['third_party', 'aggregator', 'sns', 'attachment'])
def test_non_official_fact_never_verifies(env, source_type):
    _, clock, db = env
    target = venue(db, 1)
    fact(db, target, now=kst_now(clock), source_type=source_type)
    assert reason_for(project_needs(db, target['venue_id'], now=kst_now(clock))) == 'non_official_source'


@pytest.mark.parametrize('label', ['source_claim', 'ai_inference', 'unverified', None])
def test_claims_including_legacy_null_never_verify(env, label):
    _, clock, db = env
    target = venue(db, 1)
    fact(db, target, now=kst_now(clock), label=label)
    assert reason_for(project_needs(db, target['venue_id'], now=kst_now(clock))) == 'missing_fact'


def test_latest_per_source_and_duplicate_observations(env):
    _, clock, db = env
    target = venue(db, 1)
    now = kst_now(clock)
    fact(db, target, now=now - timedelta(days=1), value='old', current=False)
    fact(db, target, now=now)
    fact(db, target, now=now + timedelta(seconds=1), obs='duplicate', current=False)
    # Current row must match the latest fact, not an obsolete observation.
    with db:
        db.execute("UPDATE venue_fields SET obs_id='duplicate'")
    result = project_needs(db, target['venue_id'], now=now)
    assert result['gap_count'] == 5 and reason_for(result) is None


def test_same_value_reobservation_does_not_invalidate_current_fact(env):
    _, clock, db = env
    target = venue(db, 1)
    now = kst_now(clock)
    original = fact(db, target, now=now)
    # Same URL/value re-observed: apply_observation keeps the original current
    # row, so its obs_id intentionally stays behind the newest observation.
    fact(db, target, now=now + timedelta(seconds=1), obs='reobserved', current=False)
    result = project_needs(db, target['venue_id'], now=now)
    assert result['gap_count'] == 5 and reason_for(result) is None
    current = db.execute(
        "SELECT obs_id FROM venue_fields WHERE venue_id=? AND field='hours' AND valid_to IS NULL",
        (target['venue_id'],)).fetchone()
    assert current['obs_id'] == original


def test_identity_type_closed_and_ig_cap(env):
    settings, _, db = env
    targets = [venue(db, n) for n in range(25)]
    closed = venue(db, 30, state='closed')
    unknown_type = venue(db, 31, kind=None)
    mismatch = {**targets[0], 'venue_key': 'mismatched'}
    watch(settings, targets + [closed, unknown_type, mismatch])
    plan = make_plan(env, targets + [closed, unknown_type, mismatch])
    assert len(plan['ig_read_list']) == 20
    assert len({r['handle'] for r in plan['ig_read_list']}) == 20
    assert not any(r['venue_id'] in ('v030', 'v031') or r['venue_key'] == 'mismatched' for r in plan['ig_read_list'])
    assert next(r for r in plan['ranked_venues'] if r['venue_key'] == 'mismatched')['reason'] == 'identity_unknown'
    assert next(r for r in plan['ranked_venues'] if r['venue_id'] == 'v031')['reason'] == 'venue_type_unknown'
    assert plan['web_targets'][-1]['venue_id'] == 'v030'


def test_model_only_breaks_ties_and_is_bounded(env, monkeypatch):
    settings, clock, db = env
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    targets = [venue(db, n) for n in range(103)]
    fact(db, targets[0], now=kst_now(clock))
    entries = watch(settings, targets)
    targets = targets[:100]
    session = Session(_envelope({'order': list(reversed(range(100)))}))
    state = State(settings.data_dir, clock)
    ranker = DeepSeekRanker(state, model=settings.model, session=session)
    rows, _, _ = snapshot(settings, targets, entries, now=kst_now(clock))
    # Lower count cannot move ahead even if the model puts it first.
    small = [rows[0], rows[1], rows[-1]]
    ranker.session = Session(_envelope({'order': [2, 1, 0]}))
    order, reason = ranker.rank(small)
    from hanset_active.collect.plan import _apply_rank
    ranked = _apply_rank(small, small, order)
    assert reason == 'model_ranked' and [r['gap_count'] for r in ranked] == [6, 6, 5]
    ranker.session = session
    plan = ensure_daily_plan(settings, state, targets, 'ok', clock=clock, planner=ranker)
    assert len(session.calls) == 1 and plan['ranking']['candidate_count'] == 100
    content = session.calls[0]['json']['messages'][1]['content']
    payload = json.loads(content)
    assert sum(len(g['venues']) for g in payload['groups']) == 100
    assert 'Fixture' not in content and 'https:' not in content and 'fixture.v' not in content and KEY not in content
    assert len(content) <= 24000
    assert set(payload) == {'groups'}
    assert session.calls[0]['json']['temperature'] == 0
    assert session.calls[0]['json']['thinking'] == {'type': 'disabled'}
    before = (settings.data_dir / 'plans' / (plan['day'] + '.json')).read_bytes()
    again = ensure_daily_plan(settings, state, targets, 'ok', clock=clock, planner=ranker)
    assert again == plan and len(session.calls) == 1
    assert (settings.data_dir / 'plans' / (plan['day'] + '.json')).read_bytes() == before


@pytest.mark.parametrize('answer', [
    {}, {'order': [0]}, {'order': [0, 0]}, {'order': [0, 2]}, {'order': [0, '1']},
    {'order': [False, 1]}, {'order': [0, 1], 'extra': 'discard'}, {'order': '0,1'},
])
def test_invalid_model_output_falls_back(env, monkeypatch, answer):
    settings, clock, db = env
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    targets = [venue(db, n) for n in range(2)]
    state = State(settings.data_dir, clock)
    session = Session(_envelope(answer))
    plan = ensure_daily_plan(settings, state, targets, 'ok', clock=clock,
                             planner=DeepSeekRanker(state, model=settings.model, session=session))
    assert plan['ranking']['reason'] == 'fallback_model_invalid'
    assert plan['ranked_venues'][0]['venue_id'] == 'v000'
    assert KEY not in json.dumps(plan)


@pytest.mark.parametrize('failure,reason', [
    ('credentials', 'fallback_missing_credentials'), ('budget', 'fallback_budget_reserved'),
    ('timeout', 'fallback_model_timeout'), ('rate', 'fallback_model_rate_limited'),
])
def test_safe_fallbacks_preserve_extraction_headroom(env, monkeypatch, failure, reason):
    settings, clock, db = env
    target = venue(db, 1)
    state = State(settings.data_dir, clock)
    if failure != 'credentials':
        monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    if failure == 'budget':
        state.data['budgets']['model_calls'] = MODEL_CALL_CAP - 1
    session = Session(_envelope({'order': [0]}))
    if failure == 'timeout':
        def fail(*args, **kwargs):
            raise requests.Timeout(KEY)
        session.post = fail
    if failure == 'rate':
        session = Session(Response(429, b'provider text'))
    plan = ensure_daily_plan(settings, state, [target], 'ok', clock=clock,
                             planner=DeepSeekRanker(state, model=settings.model, session=session))
    assert plan['ranking']['reason'] == reason
    assert KEY not in json.dumps(plan)
    if failure in ('budget', 'credentials'):
        assert not session.calls


def test_ranking_falls_back_to_qwen(env, tmp_path, monkeypatch):
    settings, clock, db = env
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    _qwen_key_file(tmp_path, monkeypatch)
    targets = [venue(db, n) for n in range(2)]
    state = State(settings.data_dir, clock)
    primary = Session(Response(429, b'provider text'))
    qwen = Session(_envelope({'order': [1, 0]}))
    monkeypatch.setattr('hanset_active.collect.deepseek.new_qwen_session', lambda: qwen)
    plan = ensure_daily_plan(settings, state, targets, 'ok', clock=clock,
                             planner=DeepSeekRanker(state, model=settings.model, session=primary))
    assert plan['ranking']['reason'] == 'model_ranked'
    # Plan artifacts keep the configured DeepSeek id even when Qwen answered.
    assert plan['ranking']['model'] == 'deepseek-flash'
    assert validate_plan(plan, plan['day'])
    assert plan['ranked_venues'][0]['venue_id'] == 'v001'
    assert len(primary.calls) == 1 and len(qwen.calls) == 1
    assert qwen.calls[0]['url'] == QWEN_ENDPOINT
    assert state.model_providers == {'qwen': 1}
    assert QWEN_KEY not in json.dumps(plan)


def test_ranking_both_fail_keeps_local_order_and_reason(env, tmp_path, monkeypatch):
    settings, clock, db = env
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    _qwen_key_file(tmp_path, monkeypatch)
    targets = [venue(db, n) for n in range(2)]
    state = State(settings.data_dir, clock)
    primary = Session(Response(429, b'provider text'))
    qwen = Session(Response(402, b'credit exhausted'))
    monkeypatch.setattr('hanset_active.collect.deepseek.new_qwen_session', lambda: qwen)
    plan = ensure_daily_plan(settings, state, targets, 'ok', clock=clock,
                             planner=DeepSeekRanker(state, model=settings.model, session=primary))
    assert plan['ranking']['reason'] == 'fallback_model_rate_limited'
    assert plan['ranked_venues'][0]['venue_id'] == 'v000'
    assert state.model_providers == {}
    assert len(primary.calls) == 1 and len(qwen.calls) == 1
    assert QWEN_KEY not in json.dumps(plan)


@pytest.mark.parametrize('calls', [MODEL_CALL_CAP - 2, MODEL_CALL_CAP - 3])
def test_ranking_headroom_reserves_two_attempts_plus_extraction(env, tmp_path, monkeypatch, calls):
    settings, clock, db = env
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    _qwen_key_file(tmp_path, monkeypatch)
    target = venue(db, 1)
    state = State(settings.data_dir, clock)
    state.data['budgets']['model_calls'] = calls
    state.save()
    primary = Session(Response(429, b'provider text'))
    qwen = Session(_envelope({'order': [0]}))
    monkeypatch.setattr('hanset_active.collect.deepseek.new_qwen_session', lambda: qwen)
    plan = ensure_daily_plan(settings, state, [target], 'ok', clock=clock,
                             planner=DeepSeekRanker(state, model=settings.model, session=primary))
    if calls == MODEL_CALL_CAP - 2:
        assert plan['ranking']['reason'] == 'fallback_budget_reserved'
        assert primary.calls == [] and qwen.calls == []
        assert state.data['budgets']['model_calls'] == calls
    else:
        assert plan['ranking']['reason'] == 'model_ranked'
        assert len(primary.calls) == 1 and len(qwen.calls) == 1
        assert state.data['budgets']['model_calls'] == calls + 2
        # One full extraction attempt still fits after both ranking attempts.
        assert state.can_model(1, OUTPUT_TOKENS)
        state.reserve_model(1, OUTPUT_TOKENS)
        assert state.data['budgets']['model_calls'] == MODEL_CALL_CAP
        assert not state.can_model(1, OUTPUT_TOKENS)


def test_run_plan_reports_transient_provider(env, tmp_path, monkeypatch):
    settings, clock, db = env
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    _qwen_key_file(tmp_path, monkeypatch)
    targets = [venue(db, n) for n in range(2)]
    save_targets(settings.data_dir / 'web_targets.json', targets)
    primary = Session(Response(429, b'provider text'))
    qwen = Session(_envelope({'order': [1, 0]}))
    monkeypatch.setattr('hanset_active.collect.deepseek.new_qwen_session', lambda: qwen)
    result = run_plan(settings, clock=clock,
                      planner=DeepSeekRanker(State(settings.data_dir, clock), model=settings.model, session=primary))
    assert result['state'] == 'done' and result['provider'] == {'qwen': 1}
    plan = json.loads((settings.data_dir / 'plans' / (result['day'] + '.json')).read_text())
    assert plan['ranking']['model'] == 'deepseek-flash' and 'provider' not in plan
    assert validate_plan(plan, plan['day'])


def test_snapshot_digest_changes_and_planning_is_read_only(env, monkeypatch):
    settings, clock, db = env
    target = venue(db, 1)
    entries = watch(settings, [target])
    live_watch = (settings.data_dir / 'watchlist.json').read_bytes()
    original = sqlite3.connect
    opened = []
    def readonly(path, *args, **kwargs):
        opened.append(str(path))
        assert 'collector.sqlite3' not in str(path)
        assert 'mode=ro' in str(path) and kwargs.get('uri') is True
        return original(path, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', readonly)
    initial = snapshot(settings, [target], entries, now=kst_now(clock))[1]
    fact(db, target, now=kst_now(clock))
    expected = list(db.iterdump())
    changed = snapshot(settings, [target], entries, now=kst_now(clock))[1]
    make_plan(env, [target])
    assert initial != changed and opened
    assert list(db.iterdump()) == expected
    assert (settings.data_dir / 'watchlist.json').read_bytes() == live_watch
    assert not (settings.data_dir / 'collector.sqlite3').exists()


@pytest.mark.parametrize('hour,minute,expected', [(12, 59, '2026-11-10'), (13, 0, '2026-11-10'),
                                                (13, 59, '2026-11-10'), (14, 0, '2026-11-11')])
def test_cli_window_day_and_freeze(env, hour, minute, expected):
    settings, clock, db = env
    watch(settings, [venue(db, 1)])
    clock.start = datetime(2026, 11, 10, hour, minute, tzinfo=KST)
    result = run_plan(settings, ranking='local', clock=clock)
    assert result['day'] == expected and result['ig_list_count'] == 1
    assert result['reason'] == ('prepared_tomorrow' if expected == '2026-11-11' else 'ok')
    plan = json.loads((settings.data_dir / 'plans' / (expected + '.json')).read_text())
    assert plan['ig_window']['start'] == expected + 'T13:00:00+09:00'
    assert plan['ig_window']['end'] == expected + 'T14:00:00+09:00'
    assert run_plan(settings, ranking='model', clock=clock) == result


@pytest.mark.parametrize('quota,halt,expected', [(19, False, 1), (20, False, 0), (0, True, 0)])
def test_ig_remaining_quota_and_halt(env, quota, halt, expected):
    settings, clock, db = env
    targets = [venue(db, n) for n in range(3)]
    watch(settings, targets)
    state = State(settings.data_dir, clock)
    state.data['counts']['ig_reads'] = quota
    if halt:
        state.halt('security_check')
    plan = ensure_daily_plan(settings, state, targets, 'ok', clock=clock, ranking='local')
    assert len(plan['ig_read_list']) == expected
    watch(settings, targets + [venue(db, 5)])
    clock.start = datetime(2026, 11, 10, 13, tzinfo=KST)
    assert ensure_daily_plan(settings, state, targets, 'ok', clock=clock, ranking='local') == plan


@pytest.mark.parametrize('halt', [False, True])
def test_tomorrow_plan_uses_fresh_quota_not_today_state(env, halt):
    settings, clock, db = env
    targets = [venue(db, n) for n in range(21)]
    watch(settings, targets)
    state = State(settings.data_dir, clock)
    state.data['counts']['ig_reads'] = 20
    if halt:
        state.halt('security_check')
    else:
        state.save()
    clock.start = datetime(2026, 11, 10, 15, tzinfo=KST)
    result = run_plan(settings, ranking='local', clock=clock)
    assert result['day'] == '2026-11-11' and result['reason'] == 'prepared_tomorrow'
    plan = json.loads((settings.data_dir / 'plans' / '2026-11-11.json').read_text())
    assert len(plan['ig_read_list']) == 20
    assert {'code': 'ig_halted_today', 'count': 1} not in plan['diagnostics']


@pytest.mark.parametrize('status', ['missing', 'empty', 'invalid'])
def test_planning_precedes_early_web_return(env, status):
    settings, clock, db = env
    watch(settings, [venue(db, 1)])
    path = settings.data_dir / 'web_targets.json'
    if status == 'empty':
        save_targets(path, [])
    elif status == 'invalid':
        path.write_text('invalid')
    result = run_web(settings, clock=clock, testing=True)
    assert result['reason'] == ('targets_invalid' if status == 'invalid' else 'no_targets')
    plan = json.loads((settings.data_dir / 'plans' / '2026-11-10.json').read_text())
    assert len(plan['ig_read_list']) == 1 and plan['targets_status'] == status
    assert {'code': 'targets_' + status, 'count': 1} in plan['diagnostics']


def test_adhoc_targets_never_read_or_write_plan(env):
    settings, clock, db = env
    target = venue(db, 1)
    other = settings.data_dir / 'other.json'
    save_targets(other, [target])
    # Even a symlinked daily plan must be irrelevant to an ad-hoc invocation.
    directory = settings.data_dir / 'plans'
    directory.mkdir()
    marker = settings.data_dir / 'sentinel'
    marker.write_text('unchanged')
    (directory / '2026-11-10.json').symlink_to(marker)
    result = run_web(settings, targets_path=other, clock=clock, testing=True)
    assert result['reason'] == 'missing_credentials'
    assert marker.read_text() == 'unchanged' and len(list(directory.iterdir())) == 1


def test_replay_is_ordering_only_and_duplicate_ids_are_coalesced(env):
    settings, clock, db = env
    a, b, c = [venue(db, n) for n in range(3)]
    plan = make_plan(env, [b, a, a])
    assert len(plan['web_targets']) == 2
    assert plan['web_targets'][0]['occurrences'] == 2
    before = (settings.data_dir / 'plans' / '2026-11-10.json').read_bytes()
    current = [c, a, a]
    ordered, notes = order_targets(local_target_order(settings, current, now=kst_now(clock)), plan)
    assert [t['venue_id'] for t in ordered] == ['v000', 'v000', 'v002']
    assert {n['code'] for n in notes} == {'removed_targets', 'unplanned_targets', 'targets_changed'}
    assert make_plan(env, current) == plan
    assert (settings.data_dir / 'plans' / '2026-11-10.json').read_bytes() == before
    changed = {**a, 'urls': list(reversed(a['urls'] + ['https://venue0.example/price']))}
    assert target_id(changed) != target_id(a)
    assert target_id({**changed, 'urls': list(reversed(changed['urls']))}) != target_id(changed)


def test_atomic_permissions_corruption_and_symlink(env):
    settings, _, db = env
    target = venue(db, 1)
    plan = make_plan(env, [target])
    path = settings.data_dir / 'plans' / '2026-11-10.json'
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    for corrupt in ('{', 'x' * 512001):
        path.write_text(corrupt)
        assert validate_plan(make_plan(env, [target]), plan['day'])
    path.unlink()
    destination = settings.data_dir / 'unrelated'
    destination.write_text('private')
    path.symlink_to(destination)
    with pytest.raises(CollectError, match='plan_symlink'):
        make_plan(env, [target])
    assert destination.read_text() == 'private'


@pytest.mark.parametrize('change', ['keys', 'auto_publish', 'timezone', 'window', 'field', 'reason', 'duplicate_target',
                                  'duplicate_read', 'handle', 'occurrences', 'reference'])
def test_strict_plan_validation(env, change):
    settings, _, db = env
    target = venue(db, 1)
    watch(settings, [target])
    plan = make_plan(env, [target])
    if change == 'keys':
        plan['extra'] = True
    elif change == 'auto_publish':
        plan['auto_publish_to_site'] = True
    elif change == 'timezone':
        plan['timezone'] = 'UTC'
    elif change == 'window':
        plan['ig_window']['end'] = plan['day'] + 'T15:00:00+09:00'
    elif change in ('field', 'reason'):
        plan['ranked_venues'][0]['fields'][0]['field_id' if change == 'field' else 'reason'] = 'unknown'
    elif change == 'duplicate_target':
        plan['web_targets'].append(copy.deepcopy(plan['web_targets'][0]))
    elif change == 'duplicate_read':
        plan['ig_read_list'].append(copy.deepcopy(plan['ig_read_list'][0]))
    elif change == 'handle':
        plan['ig_read_list'][0]['handle'] = 'accounts'
    elif change == 'occurrences':
        plan['web_targets'][0]['occurrences'] = 0
    elif change == 'reference':
        plan['web_targets'][0]['venue_id'] = 'missing'
    plan['plan_id'] = digest({k: v for k, v in plan.items() if k != 'plan_id'})
    assert not validate_plan(plan, plan['day'])


def test_incomplete_recovery_reuses_validated_decision(env, monkeypatch):
    settings, clock, db = env
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    targets = [venue(db, n) for n in range(2)]
    state = State(settings.data_dir, clock)
    session = Session(_envelope({'order': [1, 0]}))
    ranker = DeepSeekRanker(state, model=settings.model, session=session)
    import hanset_active.collect.plan as module
    write = module._write
    def fail_final(path, data):
        if path.name == '2026-11-10.json':
            raise OSError('injected crash')
        write(path, data)
    monkeypatch.setattr(module, '_write', fail_final)
    with pytest.raises(OSError):
        ensure_daily_plan(settings, state, targets, 'ok', clock=clock, planner=ranker)
    marker = settings.data_dir / 'plans' / '.incomplete-2026-11-10.json'
    assert marker.exists() and len(session.calls) == 1
    monkeypatch.setattr(module, '_write', write)
    result = ensure_daily_plan(settings, state, targets, 'ok', clock=clock, planner=ranker)
    assert result['ranking']['reason'] == 'decision_cached' and len(session.calls) == 1
    assert not marker.exists()
    assert result['ranked_venues'][0]['venue_id'] == 'v001'


def test_incomplete_without_cache_and_mismatch(env):
    settings, _, db = env
    target = venue(db, 1)
    marker = settings.data_dir / 'plans' / '.incomplete-2026-11-10.json'
    _write(marker, {'input_digest': 'mismatch', 'target_set_digest': 'mismatch'})
    plan = make_plan(env, [target])
    assert {'code': 'incomplete_discarded', 'count': 1} in plan['diagnostics']
    assert not marker.exists()


def test_process_lock_and_missing_database(env):
    settings, clock, _ = env
    with ProcessLock(settings.data_dir / 'daemon.lock'):
        assert run_plan(settings, clock=clock)['state'] == 'already_running'
    missing = CollectSettings(data_dir=settings.data_dir, active_dir=settings.active_dir / 'missing', twin_dir=settings.twin_dir)
    target = {'venue_id': 'unknown', 'venue_key': 'unknown', 'name': 'Unknown', 'urls': [], 'domains': set(), 'tavily': None}
    rows, _, notes = snapshot(missing, [target], [], now=kst_now(clock))
    assert rows[0]['gap_count'] is None and notes['active_db_missing'] == 1
    assert not missing.active_dir.exists()


def test_web_order_separate_planner_session_and_shared_budget(env, server, monkeypatch):
    settings, clock, db = env
    targets = [venue(db, n) for n in range(3)]
    for index, target in enumerate(targets):
        target['urls'] = [server.base + '/' + str(index)]
        target['domains'] = {'127.0.0.1'}
        server.add('/' + str(index), '<html><main>' + 'Official sauna information. ' * 10 + '</main></html>')
    # Venue zero has fewer gaps; it must remain last despite model position.
    fact(db, targets[0], now=kst_now(clock))
    save_targets(settings.data_dir / 'web_targets.json', targets)
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    planning = Session(_envelope({'order': [2, 1, 0]}))
    extracting = Session(_envelope({'items': [{'index': i, 'claims': []} for i in range(3)]}))
    injected = DeepSeekRanker(State(settings.data_dir, clock), model=settings.model)
    result = run_web(settings, clock=clock, session=extracting, planner=injected,
                     planner_session=planning, testing=True)
    assert result['state'] == 'done'
    assert [r['path'] for r in server.log if r['path'] != '/robots.txt'] == ['/2', '/1', '/0']
    assert len(planning.calls) == 1 and len(extracting.calls) == 1
    assert 'groups' in json.loads(planning.calls[0]['json']['messages'][1]['content'])
    assert 'text' in json.loads(extracting.calls[0]['json']['messages'][1]['content'])[0]
    assert result['budgets']['model_calls'] == 2
    assert State(settings.data_dir, clock).data['budgets']['model_calls'] == 2


def test_late_web_uses_today_and_manual_plan_uses_tomorrow(env):
    settings, clock, db = env
    watch(settings, [venue(db, 1)])
    clock.start = datetime(2026, 11, 10, 15, tzinfo=KST)
    assert run_web(settings, clock=clock, testing=True)['reason'] == 'no_targets'
    today_path = settings.data_dir / 'plans' / '2026-11-10.json'
    before = today_path.read_bytes()
    assert {'code': 'ig_window_passed', 'count': 1} in json.loads(before)['diagnostics']
    assert run_plan(settings, ranking='local', clock=clock)['day'] == '2026-11-11'
    assert today_path.read_bytes() == before


def test_missing_watchlist_and_unreadable_active_store_fall_back(env):
    settings, clock, db = env
    target = venue(db, 1)
    unreadable = CollectSettings(data_dir=settings.data_dir, active_dir=settings.active_dir / 'broken', twin_dir=settings.twin_dir)
    unreadable.active_dir.mkdir()
    (unreadable.active_dir / 'active.sqlite3').write_bytes(b'invalid sqlite')
    plan = ensure_daily_plan(unreadable, State(settings.data_dir, clock), [target], 'ok', clock=clock, ranking='local')
    assert plan['ranked_venues'][0]['gap_count'] is None and not plan['ig_read_list']
    assert {'code': 'active_db_unreadable', 'count': 1} in plan['diagnostics']
    assert {'code': 'watchlist_missing', 'count': 1} in plan['diagnostics']
