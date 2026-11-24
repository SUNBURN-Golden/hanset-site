"""Scheduler, publication and accounting tests using private temporary state."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from datetime import datetime
import hashlib
import json
import os
import signal
import subprocess
import sys
from threading import Event

import pytest
import requests

from hanset_active.collect import runtime, scheduled
from hanset_active.collect.change_feeds import ARTIFACTS, COMMON, canonical_bytes, sha
from hanset_active.collect.changes import DETECTOR_VERSION
from hanset_active.collect.config import CollectSettings
from hanset_active.collect.daily_metrics import KEYS
from hanset_active.collect.private_io import Busy, json_bytes, lock, replace
from hanset_active.collect.plan import assemble_targets
from hanset_active.collect.schedules import JOBS
from hanset_active.collect.targets import source_authorization
from hanset_active.collect.store import connect as claim_connect, digest, record
from hanset_active.config import REPO_ROOT, Settings
from hanset_active.crawl_policy import COLLECTION
from hanset_active.db import connect
from hanset_active.executor import Executor
from hanset_active.fetcher import Fetcher, USER_AGENT
from hanset_active.scheduler import enqueue
from hanset_active.util.timeutil import FakeClock, kst_now
from hanset_active.venues import upsert_venue
from hanset_twin.daemon import run_once
from hanset_twin.stages import change_detect, extract, incremental_fetch
from hanset_twin.store import connect as twin_connect
from tests.active_bot.test_collect_promotion import approval
from tests.active_bot.test_collect_web import WebModel, _hours_answer, _page, _targets, enable_qwen
from tests.active_bot.test_seed_discovery import response

A = 'https://spa.example.test/hours'
B = 'https://box.example.test/hours'
C = 'https://hall.example.test/hours'
D = 'https://club.example.test/hours'


def at(value):
    return datetime.fromisoformat('2026-11-' + value + ':00+09:00')


@pytest.fixture
def job(tmp_path, monkeypatch):
    for name in ('DEEPSEEK_API_KEY', 'TAVILY_API_KEY', 'HANSET_DEEPSEEK_MODEL', 'HANSET_COLLECT_PROMOTION_POLICY',
                 'HANSET_WEB_PROXY', 'HANSET_CATCHUP_SLOT'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('HANSET_QWEN_KEY_FILE', str(tmp_path / 'unused-qwen'))
    monkeypatch.setenv('HANSET_DATAGO_KEY_FILE', str(tmp_path / 'unused-datago'))
    settings = CollectSettings(tmp_path / 'ig', tmp_path / 'active', tmp_path / 'twin')
    return scheduled.Scheduled(tmp_path, settings, FakeClock(at('20T11:00')), testing=True)


class Wait:
    def __init__(self, finish):
        self.wait = finish


def ready(job, monkeypatch):
    monkeypatch.setenv('HANSET_AUTO_PUBLISH', '1')
    replace(job.outbox.root / 'readiness.json', json_bytes({
        'schema_version': 1, 'consumer_version': 'test-v1', 'non_publishing_check': 'passed',
        'confirmed_at': at('19T11:00').isoformat(),
    }))


def checkpoint(job, slot, request=None):
    replace(job.checkpoint, json_bytes({'slot': slot.isoformat(), 'request': request,
                                       'completed_at': kst_now(job.clock).isoformat()}))


def fake_launch(job):
    return lambda name, slot: Wait(lambda: job.run_job(name, catchup=slot)[0])


def test_watchdog_latest_strict_grace_and_rollover(job, monkeypatch):
    ready(job, monkeypatch)
    calls = []

    def start(name, slot, _):
        calls.append((name, slot))
        if name == 'wide_collect':
            checkpoint(job, slot)
        return Wait(lambda: 0)

    monkeypatch.setattr(job, 'start_payload', start)
    for now in ('20T09:20', '20T09:25'):
        job.clock.start = at(now)
        assert not job.eligible(('seed_discovery', 'wide_collect'))
    job.clock.start = at('20T11:00')
    job.watchdog(fake_launch(job))
    assert dict(calls)['seed_discovery'] == dict(calls)['wide_collect'] == at('20T09:15')
    assert len(calls) == 4
    job.watchdog(fake_launch(job))
    assert len(calls) == 4
    assert scheduled.stamp(job.stamps / 'seed_discovery.ok') == at('20T11:00')
    assert JOBS['seed_discovery'].latest(at('21T00:00')) == at('20T23:15')
    assert JOBS['daily_collect'].latest(at('21T00:00')) == at('20T04:15')


def test_admission_orders_shared_jobs_during_watchdog_and_ordinary_race(job, monkeypatch):
    ready(job, monkeypatch)
    job.clock.start = at('20T04:50')
    shared, independent = [], set()
    entered, finish, independent_done = Event(), Event(), Event()

    def start(name, slot, _):
        for path in job.payload_paths(name):
            with pytest.raises(Busy), lock(path):
                pytest.fail('payload lock was available at dispatch')
        if name in scheduled.SHARED_JOBS:
            shared.append((name, slot))
        else:
            independent.add(name)
            if len(independent) == 2:
                independent_done.set()
        if name == 'wide_collect':
            checkpoint(job, slot)
            entered.set()
            return Wait(lambda: 0 if finish.wait(timeout=10) else 1)
        return Wait(lambda: 0)

    # At :50 the latest twin slot is still inside grace. Its ordinary run is independent.
    monkeypatch.setattr(job, 'start_payload', start)
    with lock(job.settings.data_dir / 'daemon.lock'):
        job.watchdog(fake_launch(job))
        assert not set(job.stamps.glob('*.attempt')).intersection(
            job.stamps / f'{name}.attempt' for name in scheduled.SHARED_JOBS)
        assert shared == []
        assert job.run_job('active') == (0, 'older_slot_eligible')
    with ThreadPoolExecutor(3) as workers:
        first = workers.submit(job.watchdog, fake_launch(job))
        try:
            assert entered.wait(10)
            second = workers.submit(job.watchdog, fake_launch(job))
            assert job.run_job('active') == (0, 'older_slot_eligible')
            assert job.run_job('twin') == (0, 'complete')
            assert independent_done.wait(10)
            second.result(timeout=10)
        finally:
            finish.set()
        first.result(timeout=10)
    assert shared == [('wide_collect', at('20T03:15')), ('daily_collect', at('20T04:15')),
                      ('active', at('20T04:35'))]


def test_failed_ordinary_leaves_catchup_and_killed_payload_consumes_it(job, monkeypatch):
    job.clock.start = at('20T11:01')
    slot = JOBS['twin'].latest(kst_now(job.clock))
    monkeypatch.setattr(job, 'start_payload', lambda *_: Wait(lambda: 1))
    assert job.run_job('twin')[0] == 1
    assert not (job.stamps / 'twin.attempt').exists()

    def kill(_name, _slot, fds):
        return subprocess.Popen([sys.executable, '-c', 'import os,signal; os.kill(os.getpid(), signal.SIGKILL)'],
                                pass_fds=tuple(fds))

    monkeypatch.setattr(job, 'start_payload', kill)
    assert job.run_job('twin', catchup=slot)[0] == 128 + signal.SIGKILL
    assert scheduled.stamp(job.stamps / 'twin.attempt') == slot
    assert not (job.stamps / 'twin.ok').exists()
    assert job.run_job('twin', catchup=slot) == (0, 'slot_complete')


@pytest.mark.parametrize('status, success', [('partial:license_response', False), ('skipped:invalid_key', False),
                                            ('skipped:no_key', True), ('tavily_transport', False)])
def test_seed_payload_requires_complete_adapters(job, monkeypatch, status, success):
    monkeypatch.setattr(scheduled.seeds, 'discover', lambda *_, **kw: {
        'state': 'ok', 'sources': {name: {'status': status} for name in scheduled.seeds.SOURCES}})
    paths = (job.stamps / 'seed_discovery.lock', *job.payload_paths('seed_discovery'))
    with ExitStack() as stack:
        fds = [stack.enter_context(lock(path)) for path in paths]
        assert job.payload('seed_discovery', at('20T09:15'), fds) is success


def test_wrappers_manual_seed_and_twin_timeout(job, monkeypatch):
    bin_dir = job.root / 'bin'
    bin_dir.mkdir()
    trace = job.root / 'manual'
    fake = bin_dir / 'python3'
    fake.write_text(f'#!{sys.executable}\n'
                    'import os, sys\nfrom pathlib import Path\n'
                    "if sys.argv[2:5] == ['hanset_active', 'seeds', 'discover']:\n"
                    "    with Path(os.environ['TRACE']).open('a') as file:\n"
                    "        file.write(' '.join(sys.argv[5:]) + '\\n')\n"
                    'else:\n'
                    '    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n')
    fake.chmod(0o755)
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'],
               HANSET_DATA_DIR=str(job.root), HANSET_ACTIVE_DIR=str(job.settings.active_dir),
               HANSET_IG_DIR=str(job.settings.data_dir), HANSET_TWIN_DIR=str(job.settings.twin_dir), TRACE=str(trace))
    seed, twin = [REPO_ROOT / 'scripts' / f'run_{name}_cron.sh' for name in ('seed_discovery', 'twin')]
    for present in (False, True):
        if present:
            job.write_stamp('seed_discovery', 'ok', at('20T11:00'))
            job.write_stamp('seed_discovery', 'attempt', at('20T09:15'))
        before = {path.name: path.read_bytes() for path in job.stamps.glob('seed_discovery.*')}
        for args in (['--source', 'licenses'], ['--max-calls', '0']):
            assert subprocess.run([seed, *args], env=env, timeout=15).returncode == 0
        assert {path.name: path.read_bytes() for path in job.stamps.glob('seed_discovery.*')} == before
    assert trace.read_text().splitlines() == ['--source licenses', '--max-calls 0'] * 2
    timeout = bin_dir / 'timeout'
    timeout.write_text('#!/bin/sh\nexit 124\n')
    timeout.chmod(0o755)
    assert subprocess.run([twin], env=env, timeout=15).returncode == 124
    assert not (job.stamps / 'twin.ok').exists()


def test_transfer_complete_only_after_stream_finishes(job, monkeypatch):
    class Interrupted:
        status_code = 200
        headers = {'Content-Type': 'text/html'}

        def iter_content(self, _):
            yield b'incomplete'
            raise requests.exceptions.ChunkedEncodingError()

        def close(self):
            pass

    def get(session, url, **kwargs):
        assert session.headers['User-Agent'] == USER_AGENT
        return Interrupted() if url == A else response(body='Complete transfer ' * 10)

    monkeypatch.setattr(requests.Session, 'get', get)
    with closing(connect(job.metrics.active_path)) as db:
        fetcher = Fetcher(Settings(data_dir=job.settings.active_dir, testing=True), db, job.clock,
                          public_scope=True, policy=COLLECTION)
        try:
            assert fetcher.fetch(A).outcome == 'error'
            assert fetcher.fetch(B).ok
        finally:
            fetcher.close()
        assert [tuple(row) for row in db.execute(
            'SELECT status,transfer_complete FROM request_log ORDER BY request_id')] == [(200, 0), (200, 1)]


def test_web_reads_live_targets_and_drains_due_retries(job, monkeypatch):
    enable_qwen(monkeypatch)
    ready(job, monkeypatch)
    calls = []
    real_web = scheduled.run_web
    monkeypatch.setattr(scheduled, 'run_web', lambda *args, **kwargs: real_web(
        *args, **kwargs, qwen_session=WebModel(lambda items, _: _hours_answer(items))))

    def get(session, url, **options):
        calls.append(url)
        if url == C:
            return response(403)
        if url == B and job.clock.now() < at('20T05:15'):
            raise requests.Timeout()
        return response(body=_page())

    monkeypatch.setattr(requests.Session, 'get', get)
    path = job.settings.data_dir / 'web_targets.json'
    path.parent.mkdir()
    job.clock.start = at('20T04:15')
    _targets(path, A, name='Seed Spa A', official_urls=[A, B, C])
    with closing(connect(job.metrics.active_path)) as active:
        guard = scheduled.RunLock(active, job.settings.active_dir, 'test', clock=job.clock)
        with guard:
            assert not job.collect('daily_collect', at('20T04:15'), guard)
            assert calls == [A, B, C]
            job.clock.start = at('20T05:15')
            _targets(path, A, name='Seed Spa A', official_urls=[A, B, C, D])
            assert job.collect('wide_collect', at('20T05:15'), guard)
            assert calls == [A, B, C, B, D]
    with closing(claim_connect(path.parent)) as db:
        quote, locator = db.execute('SELECT quote,locator_json FROM claims WHERE source_url=?', (A,)).fetchone()
        assert quote == '' and json.loads(locator)['grounding']['version'] == 1
def export_fixture(settings, ids):
    root = settings.data_dir / 'official-web-changes'
    root.mkdir(parents=True, exist_ok=True)
    artifacts = {
        ARTIFACTS[0]: canonical_bytes({'proposals': []}),
        ARTIFACTS[1]: canonical_bytes({'candidates': [{'latest_change_ids': ids}]}),
        ARTIFACTS[2]: b'',
    }
    manifest = {**COMMON, 'generator_version': DETECTOR_VERSION, 'event_watermark': len(ids),
                'event_digest': sha(b'evidence'), 'artifacts': [
                    {'name': name, 'sha256': sha(data), 'bytes': len(data)} for name, data in artifacts.items()]}
    for name, content in artifacts.items():
        replace(root / name, content)
    value = canonical_bytes(manifest)
    replace(root / 'manifest.json', value)
    return {'reason': 'ok', 'event_watermark': len(ids), 'manifest_sha256': sha(value)}


class FakeOperations:
    """Exercise the proposed consumer contract with a fixture ledger keyed by change ID."""
    def __init__(self, outbox, current, path):
        self.outbox, self.current, self.path = outbox, current, path

    def consume(self, request):
        assert self.outbox.verify_generation(request['generation_digest']) == request['change_ids']
        self.outbox.validate_request(request)
        ledger = json.loads(self.path.read_text()) if self.path.exists() else {}
        release = []
        for change_id in request['change_ids']:
            facts = self.current[change_id]
            if (facts['official'] and facts['checked'] and facts['source'].startswith('https://')
                    and facts['mapped'] and not facts['hold'] and change_id not in ledger):
                release.append(change_id)
                ledger[change_id] = 'in_flight'
        replace(self.path, json_bytes(ledger))
        return release


def test_publication_readiness_checkpoint_and_overlapping_receipts(job, monkeypatch):
    ids = sorted(sha(value.encode()) for value in ('A', 'B', 'C', 'D', 'E'))
    a, b, c, d, e = ids
    assert job.eligible(('wide_collect',)) == []
    assert job.run_job('wide_collect', catchup=at('20T09:15'))[0] == 0
    assert not (job.stamps / 'wide_collect.attempt').exists()
    ready(job, monkeypatch)
    slot = at('20T09:15')
    export = export_fixture(job.settings, [a, b])
    request = job.outbox.prepare(job.settings.data_dir, export, slot)
    assert job.outbox.prepare(job.settings.data_dir, export, slot) == request
    checkpoint(job, slot, request)
    job.write_stamp('wide_collect', 'attempt', slot)
    expose = job.outbox.expose

    def interrupt(row):
        expose(row)
        raise OSError('simulated crash after exposure')

    monkeypatch.setattr(job.outbox, 'expose', interrupt)
    with pytest.raises(OSError):
        job.finish_wide()
    assert not (job.stamps / 'wide_collect.ok').exists()
    assert job.checkpoint.exists()
    monkeypatch.setattr(job.outbox, 'expose', expose)
    job.clock.start = at('20T11:16')
    assert job.finish_wide()
    assert scheduled.stamp(job.stamps / 'wide_collect.ok') == at('20T11:00')
    assert not job.accounted_for('wide_collect', at('20T11:15'))

    current = {cid: {'official': True, 'source': A, 'checked': '2026-11-20',
                     'mapped': True, 'hold': False} for cid in ids}
    ops = FakeOperations(job.outbox, current, job.root / 'ops' / 'ledger.json')
    assert ops.consume(request) == [a, b]
    assert ops.consume(request) == []
    current[d]['official'] = False
    current[e]['hold'] = True
    next_slot = at('20T11:15')
    second = job.outbox.prepare(job.settings.data_dir, export_fixture(job.settings, [b, c, d, e]), next_slot)
    assert FakeOperations(job.outbox, current, ops.path).consume(second) == [c]
    artifact = job.outbox.generations / second['generation_digest'] / ARTIFACTS[0]
    artifact.write_bytes(b'invalid')
    with pytest.raises(ValueError):
        job.outbox.expose(second)


def test_wide_stamps_and_metrics_close_with_absent_or_pending_receipts(job, monkeypatch):
    ready(job, monkeypatch)
    job.clock.start = at('20T23:27')
    # Isolate the wide slot from the older shared slots.
    for name in ('daily_collect', 'active', 'seed_discovery', 'twin'):
        job.write_stamp(name, 'ok', at('20T23:27'))
    slot = at('20T23:15')
    request = job.outbox.prepare(job.settings.data_dir, export_fixture(job.settings, [sha(b'A')]), slot)

    def start(name, started, fds):
        assert name == 'wide_collect'
        checkpoint(job, started, request)
        return Wait(lambda: 0)

    monkeypatch.setattr(job, 'start_payload', start)
    assert job.run_job('wide_collect', catchup=slot) == (0, 'complete')
    assert job.run_job('wide_collect', catchup=slot) == (0, 'slot_complete')
    pending = job.outbox.receipts / '2026-11-20' / request['request_id'] / 'pending.json'
    replace(pending, json_bytes({'schema_version': 1, 'request_id': request['request_id'],
                                 'generation_digest': request['generation_digest'], 'status': 'pending'}))
    job.clock.start = at('20T23:30')
    job.metrics.run()
    assert json.loads(job.metrics.output.read_text())['deploys'] == 0


def test_metrics_cutoff_rollover_and_partial_append(job, monkeypatch):
    job.clock.start = at('20T23:29')
    path = job.settings.data_dir / 'web_targets.json'
    path.parent.mkdir()
    _targets(path, A, name='Seed Spa A', venue_id='sample', venue_key='sample')
    policy = job.settings.data_dir / 'promotion_policy.json'
    replace(policy, json_bytes({'schema_version': 1, 'approvals': [approval(A)]}))
    request = job.outbox.prepare(job.settings.data_dir, export_fixture(job.settings, [sha(b'A')]), at('20T23:15'))
    job.outbox.expose(request)

    def receipt(name, status, completed, recorded, attempt):
        out = job.outbox.receipts / '2026-11-20' / request['request_id'] / f'{name}.json'
        replace(out, json_bytes({'schema_version': 1, 'request_id': request['request_id'],
                                 'generation_digest': request['generation_digest'], 'status': status,
                                 'completed_at': at(completed).isoformat(), 'recorded_at': at(recorded).isoformat(),
                                 'deployment_attempt_id': attempt}))

    receipt('one', 'deployed', '20T22:30', '20T22:35', 'deployment-a')
    receipt('replay', 'deployed', '20T22:30', '20T22:45', 'deployment-a')
    receipt('failed', 'failed', '20T23:17', '20T23:20', 'deployment-b')
    receipt('late', 'deployed', '20T23:17', '20T23:30', 'deployment-c')
    with closing(connect(job.metrics.active_path)) as writer, closing(claim_connect(path.parent)) as claims:
        with writer:
            for cid, first in (('old', at('19T12:00')), ('new', at('20T09:00'))):
                writer.execute("""INSERT INTO seed_candidates
                    (id,name,category,address,sido,sigungu,source_kind,source_url,first_seen,last_seen,status,
                     normalized_name,normalized_address) VALUES (?, 'Seed Spa A','sauna','','','','web',?,?,?,'new','','')""",
                    (cid, A, first.isoformat(), at('20T22:00').isoformat()))
            writer.executemany("""INSERT INTO request_log(url,domain,ts,status,kind,bytes,transfer_complete)
                                  VALUES (?, 'example.test',?,200,'body',5,?)""",
                               [(A, at('20T20:00').isoformat(), 1), (B, at('20T20:00').isoformat(), 0),
                                (C, at('20T23:30').isoformat(), 1)])
        target, = assemble_targets(job.settings, now=job.clock.now(), testing=True)['authorization']
        for url in (A, B, C):
            record(claims, kind='web', url=url, text_hash=digest(url), disposition='applied',
                   claims=[{'field': 'hours', 'value': {'hours': '09:00-17:00', 'scope': 'weekday'},
                            'quote': 'Sample confirmed opening hours', 'rule_id': 'sample.v1'}],
                   venue_id='sample', venue_key='sample', seen_at=job.clock.now().isoformat(), clock=job.clock,
                   **({'source_type': 'own_site', 'requested_url': A, 'final_url': A,
                       'authorization': source_authorization(target, A, A, testing=True),
                       'domains': target['domains'], 'testing': True} if url == A else {}))
        with closing(runtime.connect(job.settings.active_dir)) as models:
            attempt = runtime.dispatched(models, 'deepseek', 'sample', 'probe', job.clock, 1, 1)
            runtime.finished(models, attempt, 'model_timeout')
            runtime.dispatched(models, 'qwen', 'sample', 'completion', job.clock, 1, 1)
        tavily = job.settings.data_dir / 'tavily_usage.json'

        def usage(count):
            replace(tavily, json_bytes({'schema_version': 2, 'budgets': {'tavily': {
                'days': {'2026-11-20': {'used': count}}, 'last_reason': 'ok'}}}))

        usage(3)
        job.metrics.run()
        assert not job.metrics.output.exists()
        assert set(job.metrics._report(at('20T23:30').date(), at('20T23:30').date())) == KEYS
        job.clock.start = at('21T00:05')
        usage(4)
        replace(policy, json_bytes({'schema_version': 1, 'approvals': []}))
        replace(path, json_bytes({'schema_version': 1, 'targets': []}))
        day = at('20T23:30').date()
        report = job.metrics._report(day, day)
        assert report == {'date': '2026-11-20', 'seeds_total': 2, 'seeds_new': 1, 'targets': 1,
                          'pages_fetched': 1, 'claims_new': 3, 'facts_total': 1, 'unverified_total': 2,
                          'deploys': 1, 'deploy_failures': 1, 'provider_calls': {'deepseek': 1, 'qwen': 1},
                          'tavily_used': 3}
        line = json_bytes(report)
        replace(job.metrics.journal, json_bytes({'offset': 0, 'line': line.decode(),
                                                 'prefix_sha256': hashlib.sha256(b'').hexdigest()}))
        replace(job.metrics.output, line[:37])
        with ThreadPoolExecutor(3) as workers:
            list(workers.map(lambda _: job.metrics.run(), range(3)))
        assert job.metrics.output.read_bytes() == line
        assert json.loads(job.metrics._path(job.clock.now().date()).read_text())['targets'] == 0
        job.metrics.run()
        assert job.metrics.output.read_bytes() == line
        replace(job.metrics._path(job.clock.now().date()), b'broken')
        job.clock.start = at('21T23:30')
        job.metrics.run()
        with writer:
            writer.execute("UPDATE seed_candidates SET status='resolved' WHERE id='new'")
        assert writer.execute("SELECT status FROM seed_candidates WHERE id='new'").fetchone()[0] == 'resolved'


def test_metrics_carries_sleep_days_and_a_nonzero_unobserved_bucket_can_retry(job, capsys):
    job.clock.start = at('20T23:29')
    job.metrics.run()
    tavily = job.settings.data_dir / 'tavily_usage.json'
    replace(tavily, json_bytes({'schema_version': 2, 'budgets': {'tavily': {
        'days': {'2026-11-21': {'used': 1}}, 'last_reason': 'ok'}}}))
    job.clock.start = at('23T00:10')
    job.metrics.run()
    lines = [json.loads(line) for line in job.metrics.output.read_text().splitlines()]
    assert [line['date'] for line in lines] == ['2026-11-20', '2026-11-22']
    assert lines[1]['tavily_used'] == lines[1]['seeds_total'] == 0
    assert 'metrics_tavily_cutoff_unknown 2026-11-21' in capsys.readouterr().err


def test_guarded_active_persists_only_validated_evidence_and_twin_holds_missing(job, monkeypatch):
    settings, clock = job.settings, job.clock
    settings.active_dir.mkdir()
    calls = []

    def get(session, url, **kwargs):
        calls.append(url)
        assert session.headers['User-Agent'] == USER_AGENT
        return response(body='<title>Seed Spa A</title><main>Opening hours: 09:00-17:00. '
                             'Sensitive-fixture-body. ' + 'Guest facilities and sauna. ' * 10 + '</main>')

    monkeypatch.setattr(requests.Session, 'get', get)
    with closing(connect(job.metrics.active_path)) as db:
        vid = upsert_venue(db, 'Seed Spa A', website=A, clock=clock)
        tid = enqueue(db, kind='fetch', url=A, venue_id=vid, clock=clock)
        fetcher = Fetcher(Settings(data_dir=settings.active_dir, testing=True), db, clock,
                          public_scope=True, policy=COLLECTION)
        fetcher.facts_only = True
        try:
            executor = Executor(fetcher)
            executor.execute(dict(db.execute('SELECT * FROM tasks WHERE task_id=?', (tid,)).fetchone()))
            assert calls == [A]
            assert not (settings.active_dir / 'raw').exists()
            assert db.execute('SELECT blob_path FROM raw_documents').fetchone()[0] is None
            rows = db.execute('SELECT source_url,quote,locator FROM observations').fetchall()
            assert rows and all(row['source_url'] == A and not row['quote'] for row in rows)
            assert all(json.loads(row['locator'])['validation']['version'] == 1 for row in rows)
            assert 'Sensitive-fixture-body' not in '\n'.join(db.iterdump())
            with closing(twin_connect(settings.twin_dir / 'twin.sqlite3')) as twin:
                stages = [('fetch', incremental_fetch), ('changes', change_detect), ('extract', extract)]
                with db:
                    db.execute("UPDATE observations SET locator='{}' WHERE obs_id=(SELECT min(obs_id) FROM observations)")
                _, status, _ = run_once(twin, db, csv_path=settings.data_dir / 'absent.csv',
                                        lock_path=settings.twin_dir / 'daemon.lock', clock=clock,
                                        stages=stages, structured_only=True)
                assert status == 'ok'
                assert twin.execute("SELECT 1 FROM review_queue WHERE kind='active_validation'").fetchone()
        finally:
            fetcher.close()
