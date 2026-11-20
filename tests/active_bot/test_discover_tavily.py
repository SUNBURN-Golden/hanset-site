"""Broad discovery uses the shared private client; every POST is mocked."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
import json
import os
from pathlib import Path
import sqlite3
import traceback
from unittest.mock import Mock

import pytest

from hanset_active.discover import Discovery
from hanset_active.discover.search import Tavily, Nominatim, usable_providers, DEFAULT_PROVIDERS
from hanset_active.executor import Executor, BudgetExhausted, TaskFailure
from hanset_active.fetcher import FetchResult
from hanset_active.gaps import ensure_gaps
from hanset_active.runner import run
from hanset_active.scheduler import Scheduler, enqueue
from hanset_active.search import discover_known_venue, tavily, budget
from hanset_active.util.timeutil import kst_now
from hanset_active.venues import upsert_venue

SENTINEL = 'sentinel-broad-discovery-private-value'


def response(rows=(), status=200, headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.iter_content.return_value = [json.dumps({'results': list(rows)}).encode()]
    return result


@pytest.fixture(autouse=True)
def private_client(monkeypatch, isolate_tavily_credentials, bot):
    monkeypatch.setenv('TAVILY_API_KEY', SENTINEL)
    post = Mock(return_value=response())
    session = Mock(post=post)
    monkeypatch.setattr(tavily.requests, 'Session', lambda: session)
    return post


def provider(bot, **kwargs):
    context = Discovery(bot, budget=100)
    return Tavily(context.get, bot.db, clock=bot.clock, **kwargs)


def block_others(bot):
    for name in DEFAULT_PROVIDERS[1:]:
        bot.db.execute('INSERT OR REPLACE INTO provider_state VALUES (?,?,?,?,?)',
                       (name, 'disabled', 'fixture block', None, kst_now(bot.clock).isoformat()))
    bot.db.commit()


def task(bot, tid):
    return dict(bot.db.execute('SELECT * FROM tasks WHERE task_id=?', (tid,)).fetchone())


def usage_path():
    return Path(os.environ['HANSET_IG_DIR']) / 'tavily_usage.json'


def profile_for_directory(bot):
    profile = bot.settings.profile
    profile['regions'] = [dict(profile['regions'][0], id='first', osm_city='Seoul'),
                          dict(profile['regions'][0], id='second', osm_city='Busan')]
    profile['venue_types'] = profile['venue_types'][:1]
    path = bot.settings.data_dir / 'directory-profile.json'
    path.write_text(json.dumps(profile))
    bot.settings.profile_path = path


def known_gap(bot):
    vid = upsert_venue(bot.db, 'Silver Sauna', website='https://silver.example', city='Seoul')
    ensure_gaps(bot.db, vid, clock=bot.clock)
    return vid, 'price_day_pass'


def test_default_order_and_explicit_order(bot, private_client):
    assert usable_providers(bot.db, clock=bot.clock) == ['tavily', 'nominatim', 'wikipedia', 'ddg_html']
    assert [p.name for p in Executor(bot).providers] == list(DEFAULT_PROVIDERS)
    assert usable_providers(bot.db, clock=bot.clock, names=['ddg_html', 'tavily']) == ['ddg_html', 'tavily']
    assert usable_providers(bot.db, names=[]) == []
    assert not usage_path().exists()
    private_client.assert_not_called()


@pytest.mark.parametrize('value,reason', [(None, 'tavily_key_missing'), ('invalid value', 'tavily_key_invalid')])
def test_missing_invalid_and_reprovisioned_key(bot, monkeypatch, private_client, value, reason):
    if value is None:
        monkeypatch.delenv('TAVILY_API_KEY')
    else:
        monkeypatch.setenv('TAVILY_API_KEY', value)
    assert usable_providers(bot.db, clock=bot.clock)[0] == 'nominatim'
    p = provider(bot)
    assert p.search('Seoul sauna') == [] and not p.completed
    assert bot.db.execute("SELECT reason FROM provider_state WHERE provider='tavily'").fetchone()[0] == reason
    assert not usage_path().exists()
    private_client.assert_not_called()
    monkeypatch.setenv('TAVILY_API_KEY', SENTINEL)
    assert usable_providers(bot.db, clock=bot.clock)[0] == 'tavily'


def test_existing_loader_file_precedence_and_manual_blocks(bot, monkeypatch, private_client):
    tavily._KEY_PATH.write_text('file-fixture-private-value')
    tavily._KEY_PATH.chmod(0o600)
    monkeypatch.setenv('TAVILY_API_KEY', 'invalid value')
    assert usable_providers(bot.db, clock=bot.clock)[0] == 'tavily'
    assert provider(bot).search('Seoul sauna') == []
    assert private_client.call_args.kwargs['headers']['Authorization'] == 'Bearer file-fixture-private-value'
    until = (kst_now(bot.clock) + timedelta(hours=1)).isoformat()
    bot.db.execute("UPDATE provider_state SET state='disabled',reason='operator block',next_at=? WHERE provider='tavily'", (until,))
    bot.db.commit()
    assert 'tavily' not in usable_providers(bot.db, clock=bot.clock)
    assert provider(bot).search('Seoul sauna') == []
    assert private_client.call_count == 1
    bot.clock.sleep(3600)
    assert usable_providers(bot.db, clock=bot.clock)[0] == 'tavily'


@pytest.mark.parametrize('query', [None, 42, '', ' ', 'x' * 241, 'RUNPORT sauna',
                                   'sauna\nSeoul', 'sauna api_key=synthetic', 'sauna hidden%40example.test'])
def test_bad_queries_do_not_load_credentials_or_reserve(bot, monkeypatch, private_client, query):
    loader = Mock(side_effect=AssertionError('credential access before validation'))
    monkeypatch.setattr(tavily, '_load_credential', loader)
    assert tavily.discover_query(query).reason == 'tavily_invalid_query'
    assert provider(bot).search(query) == []
    loader.assert_not_called()
    private_client.assert_not_called()
    assert not usage_path().exists()


def test_shared_collector_and_discovery_account_for_all_calls(bot, private_client):
    gap = known_gap(bot)
    p = provider(bot)
    for index in range(32):
        if index % 2:
            assert p.search('Seoul sauna') == [] and p.completed
        else:
            assert discover_known_venue(bot.db, *gap, clock=bot.clock).status == 'ok'
    assert private_client.call_count == 32
    assert json.loads(usage_path().read_text())['budgets']['tavily']['days']['2026-01-01']['used'] == 32
    bot.clock.sleep(24 * 3600)
    assert budget.availability(clock=bot.clock).snapshot.used == 0
    assert provider(bot).search('Seoul sauna') == []
    assert private_client.call_count == 33


def test_concurrent_broad_and_collector_share_ledger(bot, private_client):
    vid = upsert_venue(bot.db, 'Seed Spa A', website='https://spa.example.test', city='Seoul')
    ensure_gaps(bot.db, vid, clock=bot.clock)
    bot.db.commit()
    db_uri = (bot.settings.data_dir / 'test.sqlite3').as_uri() + '?mode=ro'

    def search(index):
        if index % 2:
            return tavily.discover_query('Seoul sauna', clock=bot.clock)
        with closing(sqlite3.connect(db_uri, uri=True)) as db:
            db.row_factory = sqlite3.Row
            return discover_known_venue(db, vid, 'price_day_pass', clock=bot.clock)

    with ThreadPoolExecutor(4) as pool:
        outcomes = list(pool.map(search, range(40)))
    assert sum(o.status == 'ok' for o in outcomes) == private_client.call_count == 40
    assert json.loads(usage_path().read_text())['budgets']['tavily']['days']['2026-01-01']['used'] == 40


def test_availability_is_non_reserving_and_cooldown_recovers(bot, private_client):
    assert budget.availability(clock=bot.clock).snapshot.used == 0
    assert not usage_path().exists()
    private_client.return_value = response(status=429, headers={'Retry-After': '120'})
    assert tavily.discover_query('Seoul sauna', clock=bot.clock).reason == 'tavily_rate_limited'
    available = budget.availability(clock=bot.clock)
    assert available.reason == 'tavily_cooldown' and available.snapshot.used == 1
    assert available.retry_at == (kst_now(bot.clock) + timedelta(seconds=120)).isoformat()
    assert 'tavily' not in usable_providers(bot.db, clock=bot.clock)
    bot.clock.sleep(120)
    assert usable_providers(bot.db, clock=bot.clock)[0] == 'tavily'
    assert bot.db.execute("SELECT next_at FROM provider_state WHERE provider='tavily'").fetchone()[0] is None


@pytest.mark.parametrize('failure', ['corrupt', 'write'])
def test_bad_state_or_failed_persistence_fails_closed(bot, monkeypatch, private_client, failure):
    if failure == 'corrupt':
        budget.availability(clock=bot.clock)
        usage_path().write_text('broken')
        usage_path().chmod(0o600)
        assert budget.availability(clock=bot.clock).reason == 'tavily_budget_unavailable'
    else:
        monkeypatch.setattr(budget, '_write', Mock(side_effect=OSError(SENTINEL)))
    assert provider(bot).search('Seoul sauna') == []
    private_client.assert_not_called()
    assert SENTINEL not in '\n'.join(bot.db.iterdump())


@pytest.mark.parametrize('url,title', [
    ('https://naver.com/sauna', ''), ('https://sub.naver.me/sauna', ''),
    ('https://www.instagram.com/sauna/', ''), ('https://silver.example/RUNPORT', ''),
    ('https://silver.example', 'RUNPORT sauna'), ('https://localhost/sauna', ''),
    ('https://192.168.1.1/sauna', ''), ('https://silver.example/?token=synthetic', ''),
    ('https://user:pass' + chr(64) + 'silver.example', ''),
])
def test_denied_private_and_credential_urls_make_no_candidates(bot, private_client, url, title):
    block_others(bot)
    private_client.return_value = response([{'url': url, 'title': title, 'content': 'Seoul sauna'}])
    tid = enqueue(bot.db, kind='search', payload={'query': 'Seoul sauna'})
    Executor(bot).execute(task(bot, tid))
    assert not bot.db.execute("SELECT 1 FROM tasks WHERE kind='trial'").fetchone()
    assert not bot.db.execute('SELECT 1 FROM venues').fetchone()


@pytest.mark.parametrize('kind,payload', [
    ('search', {'query': 'Seoul sauna'}),
    ('directory', {'query': 'Seoul sauna'}),
    ('directory', {'kind': 'osm_search', 'url': 'https://nominatim.openstreetmap.org/search', 'paginate': True}),
])
def test_pending_jobs_reach_tavily_and_snippets_stay_hints(bot, private_client, kind, payload):
    block_others(bot)
    profile_for_directory(bot)
    private_client.return_value = response([{'url': 'https://silver.example/sauna', 'title': 'Silver Sauna Seoul',
                                            'content': 'Day pass 25000 KRW. Opening hours 09:00 ~ 22:00.'}])
    tid = enqueue(bot.db, kind=kind, payload=payload)
    ex = Executor(bot)
    ex.execute(task(bot, tid))
    trial = bot.db.execute("SELECT * FROM tasks WHERE kind='trial'").fetchone()
    assert trial and 'tavily' in trial['discovered_via'] and trial['venue_id'] is None
    assert trial['payload'] == '{}'
    for table in ('venues', 'observations', 'venue_fields', 'raw_documents', 'extractions'):
        assert not bot.db.execute('SELECT 1 FROM ' + table).fetchone()
    assert ex.requests == private_client.call_count == (2 if payload.get('kind') == 'osm_search' else 1)
    for call in private_client.call_args_list:
        assert call.args == (tavily.ENDPOINT,)
        assert 'offset' not in call.kwargs['json'] and 'nominatim' not in call.kwargs['json']['query']


def test_provider_pins_and_injected_providers_remain_authoritative(bot, private_client):
    fake = Mock(name='injected')
    fake.name = 'injected'
    fake.completed = True
    fake.search.return_value = []
    tid = enqueue(bot.db, kind='search', payload={'query': 'Seoul sauna', 'provider': 'injected'})
    assert run(bot, initialize=False, providers=[fake], max_requests=2)['completed'] == 1
    fake.search.assert_called_once()
    assert task(bot, tid)['state'] == 'done'
    private_client.assert_not_called()


@pytest.mark.parametrize('pinned', [False, True])
def test_nominatim_fallback_and_pin_skip_tavily(bot, monkeypatch, private_client, pinned):
    if not pinned: monkeypatch.delenv('TAVILY_API_KEY')
    body = json.dumps([{'class': 'leisure', 'name': 'Silver Sauna',
                       'display_name': 'Seoul sauna', 'extratags': {'website': 'https://silver.example'}}]).encode()
    getter = Mock(return_value=FetchResult('https://nominatim.openstreetmap.org/search', 'ok', content=body))
    osm = Nominatim(getter, bot.db, clock=bot.clock)
    payload = {'kind': 'osm_search', 'url': 'https://nominatim.openstreetmap.org/search'}
    if pinned: payload['provider'] = 'nominatim'
    tid = enqueue(bot.db, kind='directory', payload=payload)
    ex = Executor(bot, providers=[provider(bot), osm])
    ex.context.profile['regions'] = ex.context.profile['regions'][:1]
    ex.context.profile['venue_types'] = ex.context.profile['venue_types'][:1]
    ex.execute(task(bot, tid))
    getter.assert_called_once()
    private_client.assert_not_called()
    assert bot.db.execute('SELECT discovered_via FROM venues').fetchone()[0] == 'directory:osm_search'


def test_injected_tavily_still_accounts_and_hit_identity_stays_empty(bot, private_client):
    private_client.return_value = response([{'url': 'https://silver.example/sauna', 'title': 'Seoul sauna',
                                            'content': 'Day pass 25000 KRW'}])
    p = provider(bot)
    hit = p.search('Seoul sauna')[0]
    assert hit.name == hit.address == hit.website == ''
    assert hit.extra == {'provider': 'tavily', 'verification': 'unverified'}
    tid = enqueue(bot.db, kind='search', payload={'query': 'Seoul sauna'})
    ex = Executor(bot, providers=[p], max_requests=1)
    ex.execute(task(bot, tid))
    assert ex.requests == 1 and ex.context.remaining == 0
    with pytest.raises(BudgetExhausted): ex.execute(task(bot, tid))
    assert private_client.call_count == 2


@pytest.mark.parametrize('limit', ['requests', 'deadline', 'context'])
def test_no_dispatch_past_run_limits(bot, private_client, limit):
    tid = enqueue(bot.db, kind='search', payload={'query': 'Seoul sauna', 'provider': 'tavily'})
    ex = Executor(bot, max_requests=0 if limit == 'requests' else 10,
                  deadline=0 if limit == 'deadline' else None)
    ex.heartbeat = Mock()
    if limit == 'context': ex.context.remaining = 0
    with pytest.raises(BudgetExhausted): ex.execute(task(bot, tid))
    assert ex.requests == 0 and not usage_path().exists()
    private_client.assert_not_called()


def test_guard_rechecks_deadline_after_reservation_and_heartbeats(bot, monkeypatch, private_client):
    tid = enqueue(bot.db, kind='search', payload={'query': 'Seoul sauna', 'provider': 'tavily'})
    session = Mock(post=private_client)
    def setup():
        bot.clock.sleep(2)
        return session
    monkeypatch.setattr(tavily.requests, 'Session', setup)
    ex = Executor(bot, deadline=1)
    ex.heartbeat = Mock()
    with pytest.raises(BudgetExhausted): ex.execute(task(bot, tid))
    assert ex.heartbeat.call_count >= 2 and ex.requests == 0
    assert budget.availability(clock=bot.clock).snapshot.used == 1
    private_client.assert_not_called()


@pytest.mark.parametrize('status', [200, 503])
def test_runner_counts_empty_or_failed_dispatch_once(bot, private_client, status):
    block_others(bot)
    tid = enqueue(bot.db, kind='search', payload={'query': 'Seoul sauna'})
    private_client.return_value = response(status=status)
    outcome = run(bot, initialize=False, max_requests=3)
    assert outcome['requests'] == private_client.call_count == 1
    assert task(bot, tid)['state'] == ('done' if status == 200 else 'pending')
    assert task(bot, tid)['attempts'] == 0
    assert not bot.db.execute('SELECT 1 FROM failures').fetchone()
    row = bot.db.execute('SELECT * FROM request_log').fetchone()
    assert row['url'] == tavily.ENDPOINT and row['run_id'] == outcome['run_id']


def test_cooldown_race_defers_pending_job_without_failure(bot, monkeypatch, private_client):
    block_others(bot)
    tid = enqueue(bot.db, kind='search', payload={'query': 'Seoul sauna'})
    original = tavily.discover_query
    def race(*args, **kwargs):
        with budget.locked_budget(clock=bot.clock) as reservation:
            assert reservation.reserve()
            reservation.finish('tavily_rate_limited', 120)
        return original(*args, **kwargs)
    monkeypatch.setattr(tavily, 'discover_query', race)
    outcome = run(bot, initialize=False, max_requests=3)
    assert outcome['requests'] == 0 and outcome['completed'] == 0
    row = task(bot, tid)
    assert row['state'] == 'pending' and row['attempts'] == 0 and row['lease_until'] is None
    assert row['next_at'] == (kst_now(bot.clock) + timedelta(seconds=120)).isoformat()
    assert not bot.db.execute('SELECT 1 FROM failures').fetchone()
    private_client.assert_not_called()


@pytest.mark.parametrize('interruption', ['cooldown', 'run'])
def test_osm_checkpoint_resumes_at_next_combination(bot, private_client, interruption):
    block_others(bot)
    profile_for_directory(bot)
    tid = enqueue(bot.db, kind='directory', payload={'kind': 'osm_search', 'url': 'https://unused.example/search'})
    if interruption == 'cooldown':
        private_client.side_effect = [response(), response(status=429, headers={'Retry-After': '120'}), response()]
    first = run(bot, initialize=False, max_requests=1 if interruption == 'run' else 10)
    assert first['requests'] == (1 if interruption == 'run' else 2) and first['completed'] == 0
    assert task(bot, tid)['state'] == 'pending' and task(bot, tid)['attempts'] == 0
    assert bot.db.execute("SELECT cursor FROM run_checkpoints WHERE kind='discovery:osm'").fetchone()[0] == '1'
    if interruption == 'cooldown': bot.clock.sleep(120)
    second = run(bot, initialize=False, max_requests=10)
    assert second['requests'] == 1 and second['completed'] == 1
    assert private_client.call_count == (2 if interruption == 'run' else 3) and task(bot, tid)['state'] == 'done'
    queries = [call.kwargs['json']['query'] for call in private_client.call_args_list]
    assert 'Seoul' in queries[0] and all('Busan' in query for query in queries[1:])


def test_provider_recovery_clears_only_provider_deferrals(bot, monkeypatch):
    block_others(bot)
    monkeypatch.delenv('TAVILY_API_KEY')
    tid = enqueue(bot.db, kind='search', payload={'query': 'Seoul sauna', 'provider': 'tavily'})
    scheduler = Scheduler(bot.db, clock=bot.clock)
    assert scheduler.pick() is None and task(bot, tid)['next_at']
    monkeypatch.setenv('TAVILY_API_KEY', SENTINEL)
    assert scheduler.pick()['task_id'] == tid
    assert task(bot, tid)['next_at'] is None
    future = (kst_now(bot.clock) + timedelta(hours=1)).isoformat()
    bot.db.execute('UPDATE tasks SET next_at=? WHERE task_id=?', (future, tid))
    bot.db.commit()
    assert Scheduler(bot.db, clock=bot.clock).pick() is None
    assert task(bot, tid)['next_at'] == future


@pytest.mark.parametrize('echo', ['response', 'exception'])
def test_secret_echo_never_enters_hits_state_or_rendered_errors(bot, private_client, caplog, capsys, echo):
    block_others(bot)
    if echo == 'response':
        encoded = ''.join('%%%02X' % byte for byte in SENTINEL.encode())
        private_client.return_value = response([{'url': 'https://silver.example/sauna',
            'title': 'Seoul sauna ' + SENTINEL, 'content': encoded}])
    else:
        private_client.side_effect = RuntimeError(SENTINEL)
    p = provider(bot)
    hits = p.search('Seoul sauna')
    assert SENTINEL not in repr(hits)
    tid = enqueue(bot.db, kind='search', payload={'query': 'Seoul sauna', 'provider': 'tavily'})
    rendered = ''
    try:
        Executor(bot).execute(task(bot, tid))
    except Exception as error:
        rendered = ''.join(traceback.format_exception(error))
    persisted = '\n'.join(bot.db.iterdump()) + usage_path().read_text()
    assert SENTINEL not in persisted + rendered + caplog.text + str(capsys.readouterr())


def test_subsequent_trial_still_obeys_robots(bot, monkeypatch, private_client):
    block_others(bot)
    private_client.return_value = response([{'url': 'https://silver.example/private', 'title': 'Seoul sauna'}])
    tid = enqueue(bot.db, kind='search', payload={'query': 'Seoul sauna'})
    Executor(bot).execute(task(bot, tid))
    trial = dict(bot.db.execute("SELECT * FROM tasks WHERE kind='trial'").fetchone())
    robots = Mock(status_code=200, headers={'Content-Type': 'text/plain'})
    robots.iter_content.return_value = [b'User-agent: *\nDisallow: /private\n']
    get = Mock(return_value=robots)
    monkeypatch.setattr(bot.session, 'get', get)
    ex = Executor(bot)
    ex.install()
    try:
        with pytest.raises(TaskFailure, match='robots_disallowed'):
            ex.execute(trial)
    finally:
        ex.close()
    get.assert_called_once()
    assert get.call_args.args[0] == 'https://silver.example/robots.txt'
    assert not bot.db.execute('SELECT 1 FROM observations').fetchone()
