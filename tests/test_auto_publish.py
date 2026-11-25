"""Offline publication integration against a real Outbox and fake uploader."""
from contextlib import closing
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from scripts import auto_publish as publisher
from hanset_active.collect import change_feeds
from hanset_active.collect.handoff import Outbox
from hanset_active.collect.private_io import lock
from hanset_active.collect.store import connect
import release_manifest as rm
from tests.active_bot.test_collect_provenance import OLD_SCHEMA

ROOT = Path(__file__).resolve().parents[1]
SLUG = 'seed-spa-a'
NAME = 'Seed Spa A'
URL = 'https://example.test/visit'
TARGET = {'venue_id': 'venue-a', 'venue_key': SLUG, 'urls': [URL], 'domains': {'example.test'}}
DAY = datetime(2026, 11, 20, 4, 15, tzinfo=publisher.KST)
TEST_SECRET = 'synthetic-provider-sentinel'
CONSENT = {'schema_version': 1, 'venues': [{
    'venue_id': 'declined', 'venue_key': 'declined', 'slugs': ['runport-mapo'],
    'names': ['RUNPORT'], 'aliases': ['Box B'], 'official_urls': ['https://declined.example.test/branch'],
    'decision': 'declined', 'decided_on': '2026-10-19',
}]}

# The build shim emits an artifact consumed by the real manifest, privacy and
# shape gates. It also detects a credential or managed date reaching pytest.
BUILD_SHIM = r"""
import json
import os
from pathlib import Path
import shutil
import sys

area = Path(__file__).parent
control = area / 'fault'
fault = control.read_text() if control.exists() else ''
assert 'TEST_PROVIDER_CREDENTIAL' not in os.environ
if sys.argv[1:3] == ['-m', 'pytest']:
    assert not any(k.startswith('HANSET_PUBLISH_') or k == 'HANSET_RELEASE_DATE' for k in os.environ)
    with (area / 'calls').open('a') as stream:
        stream.write('pytest\n')
    sys.exit(1 if fault == 'pytest' else 0)
if sys.argv[1].endswith('release_contract.py'):
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
assert '--release' in sys.argv and os.environ['HANSET_RELEASE_DATE']
with (area / 'calls').open('a') as stream:
    stream.write('build\n')
dist = Path(sys.argv[sys.argv.index('--out') + 1]) if '--out' in sys.argv else Path('site/dist')
if dist.exists():
    shutil.rmtree(dist)
dist.mkdir(parents=True)
venue = bool(json.loads(Path('site/content/venues_en.json').read_text()))
body = Path('saunas.csv').read_text() + Path('site/content/closed_days.json').read_text()
if fault == 'leak':
    body += '<p>' + '@'.join(('private', 'example.test')) + '</p>'
if venue and fault != 'sanity':
    (dist / 'finder/seed-spa-a').mkdir(parents=True)
    (dist / 'finder/seed-spa-a/index.html').write_text('venue')
if fault != 'sanity':
    for index in range(20):
        (dist / f'page-{index}.html').write_text('page')
(dist / 'index.html').write_text(body)
(dist / '404.html').write_text('404')
(dist / 'google1cc2d472584fa095.html').write_text('verification')
(dist / 'robots.txt').write_text('Disallow: /' if fault == 'shape' else 'Allow: /')
(dist / 'sitemap.xml').write_text(
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    '<url><loc>https://hanset.soulbound.co.kr/</loc></url></urlset>'
)
"""

DEPLOY_SHIM = r"""
import os
from pathlib import Path
import shutil
import subprocess
import sys

area = Path(__file__).parent
control = area / 'fault'
fault = control.read_text() if control.exists() else ''
assert 'TEST_PROVIDER_CREDENTIAL' not in os.environ
with (area / 'calls').open('a') as stream:
    stream.write('deploy\n')
python = os.environ['HANSET_RELEASE_PYTHON']
fd = int(os.environ['HANSET_PUBLISH_LOCK_FD'])
def run(*args):
    subprocess.run([python, *args], pass_fds=(fd,), check=True)
run('scripts/release_contract.py', 'check-lock')
run('site/build_site.py', '--release')
dist = Path.cwd() / 'site/dist'
if fault == 'pre-upload':
    sys.exit(1)
if fault == 'drift':
    (dist / 'index.html').write_text('changed artifact')
run('scripts/release_contract.py', 'started', str(dist))
live = area / 'live'
if live.exists():
    shutil.rmtree(live)
shutil.copytree(dist, live)
if fault == 'uncertain':
    sys.exit(1)
run('scripts/release_contract.py', 'confirmed')
if fault == 'confirmed-crash':
    sys.exit(1)
if fault == 'checkout-interrupted':
    (area / 'checkout/saunas.csv').write_text('concurrent edit')
"""


def executable(path, body):
    path.write_text(f'#!{sys.executable}\n{body}')
    path.chmod(0o700)
    return path


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    rm.atomic_json(path, data)


@pytest.fixture(autouse=True)
def disable_provider_files(monkeypatch):
    monkeypatch.setenv('HANSET_QWEN_KEY_FILE', '/dev/null')
    monkeypatch.setenv('HANSET_DATAGO_KEY_FILE', '/dev/null')


@pytest.fixture
def ops(tmp_path):
    root, data = tmp_path / 'checkout', tmp_path / 'data'
    content = root / 'site/content'
    put(content / 'foreigner.json', {})
    put(content / 'closed_days.json', {})
    put(content / 'events.json', {})
    row = {'title': NAME, 'slug': SLUG, 'csv': 'saunas', 'facts': [['요금', '{price}']]}
    put(content / 'venues.json', {NAME: row})
    put(content / 'venues_en.json', {SLUG: {**row, 'facts': [['Price', '{price}']]}})
    (root / 'boxes.csv').write_text('name,website,drop_in_price_krw\n')
    (root / 'events.csv').write_text('name\n')
    (root / 'saunas.csv').write_text(f'name,website,price_krw\n{NAME},{URL},15000\n')
    put(root / 'data/pipeline/queue.json', {'stages': {'4_published_or_expired': {
        'excluded_declined_venues': [{'slug': 'runport-mapo', 'reason': 'Declined 2026-10-19'}],
    }}})
    for name in ('site/build_site.py', 'site/verified.py', 'site/release_manifest.py',
                 'site/release_artifact.py', 'scripts/release_contract.py'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, path)
    subprocess.run(['git', 'init', '-q', root], check=True)
    subprocess.run(['git', '-C', root, 'add', '.'], check=True)

    deploy = executable(tmp_path / 'fake deploy & :', DEPLOY_SHIM)
    deploy.with_name(deploy.name + '.hanset-contract').write_text(publisher.CONTRACT)
    launcher = executable(tmp_path / 'launcher', 'raise AssertionError("manual launcher called")')
    python = executable(tmp_path / 'python', BUILD_SHIM)
    env = {'PATH': os.environ.get('PATH', os.defpath), 'HANSET_SITE_ROOT': str(root), 'HANSET_DATA_DIR': str(data),
           'HANSET_RELEASE_PYTHON': str(python), 'HANSET_DEPLOY_CMD': str(deploy),
           'HANSET_DEPLOY_LAUNCHER': str(launcher), 'TEST_PROVIDER_CREDENTIAL': TEST_SECRET}
    outbox = Outbox(data)
    put(outbox.root / 'consent.json', CONSENT)
    put(data / 'ig/site_slug_map.json', {'venue-a': {'venue_key': SLUG, 'site_slug': SLUG}})
    for subdir, name in zip(('active', 'twin'), publisher.DB_NAMES):
        (data / subdir).mkdir(parents=True)
        with closing(sqlite3.connect(data / subdir / name)) as db:
            db.execute('CREATE TABLE sample (value INTEGER)')
    with closing(sqlite3.connect(data / 'ig/collector.sqlite3')) as legacy, legacy:
        legacy.executescript(OLD_SCHEMA)
        content_hash = rm.digest(b'legacy-fixture')
        legacy.execute('INSERT INTO receipts VALUES (?, ?, ?, ?, ?, ?)',
                       (content_hash, 'web', URL, content_hash, 'applied', DAY.isoformat()))
        legacy.execute("""INSERT INTO claims VALUES
                       (?, ?, 'venue-a', ?, 'hours', '{}', 'legacy fixture', ?, 'web', 'own_site',
                        'source_claim', 'internal_review_only', 'synthetic', ?, ?)""",
                       (content_hash, content_hash, SLUG, URL, DAY.isoformat(), content_hash))
    with closing(connect(data / 'ig')) as db:
        publisher.synthetic_observation(db, TARGET, DAY, {'ticket': 'day_pass', 'amount': 15000})
    baseline = tmp_path / 'baseline'
    subprocess.run([python, 'site/build_site.py', '--release', '--out', baseline], cwd=root,
                   env={**os.environ, 'HANSET_RELEASE_DATE': DAY.date().isoformat()}, check=True)
    (tmp_path / 'calls').unlink()
    return root, data, outbox, baseline, env


def run(ops, *args):
    result = subprocess.run([ROOT / 'scripts/auto_publish.sh', *args], env=ops[-1], capture_output=True,
                            text=True, timeout=30)
    assert TEST_SECRET not in result.stdout + result.stderr
    return result


def activate(ops):
    result = run(ops, '--check-readiness', '--deployed-artifact', ops[3])
    assert result.returncode == 0, result.stderr
    assert ops[2].ready()


def batch(ops, value, day=1, *, job='daily_collect', field='price_day_pass', **kwargs):
    _, data, outbox, _, _ = ops
    at = DAY + timedelta(minutes=day)
    value = {'amount': value, 'ticket': 'day_pass'} if field == 'price_day_pass' else value
    with closing(connect(data / 'ig')) as db:
        publisher.synthetic_observation(db, TARGET, at, value, field=field, **kwargs)
        export = change_feeds.export_changes(db, data / 'ig')
    request = outbox.prepare(data / 'ig', export, at, job=job)
    if request:
        outbox.expose(request)
    return request


def baseline(ops):
    return (ops[2].root / 'last-deployed.json').read_bytes()


def calls(ops):
    path = ops[3].parent / 'calls'
    return path.read_text().splitlines() if path.exists() else []


def fault(ops, value):
    (ops[3].parent / 'fault').write_text(value)


def receipts(ops, request):
    return [rm.read_json(path) for path in ops[2].receipts.glob(f'*/{request["request_id"]}/*.json')]


def test_confirmed_overlapping_replays_same_day_and_unchanged(ops):
    activate(ops)
    first = batch(ops, 17000)
    second = batch(ops, 17000, 2, job='wide_collect')
    assert first['change_ids'] == second['change_ids'] and first['request_id'] != second['request_id']
    twin = ops[1] / 'twin/twin.sqlite3'
    before_twin = twin.read_bytes()
    binding = ops[1] / 'ig/collector.sqlite3'
    before_collector = binding.read_bytes()
    result = run(ops)
    assert result.returncode == 0, result.stderr
    assert calls(ops) == ['build', 'pytest', 'deploy', 'build']
    assert twin.read_bytes() == before_twin and binding.read_bytes() == before_collector
    for request in (first, second):
        assert {r['status'] for r in receipts(ops, request)} == {'pending', 'deployed'}
    assert '17000' in (ops[0] / 'saunas.csv').read_text()
    backup = next((ops[1] / 'backups').glob('publish-*'))
    assert rm.read_json(backup / 'manifest.json').keys() == set(publisher.DB_NAMES)
    before = baseline(ops)
    assert run(ops, '--request', first['request_id']).returncode == 0
    assert calls(ops).count('deploy') == 1 and baseline(ops) == before
    third = batch(ops, 17000, 3)
    assert run(ops).returncode == 0
    assert [row['status'] for row in receipts(ops, third)] == ['no_change']
    assert calls(ops) == ['build', 'pytest', 'deploy', 'build']
    fourth = batch(ops, 19000, 4)
    assert run(ops).returncode == 0 and calls(ops).count('deploy') == 2
    assert '19000' in (ops[0] / 'saunas.csv').read_text() and baseline(ops) != before
    ledger = rm.read_json(ops[2].root / 'ledger.json')['changes']
    assert set(ledger) == set(first['change_ids'] + fourth['change_ids'])
    assert all(row['disposition'] == 'deployed' for row in ledger.values())
    assert TEST_SECRET not in ''.join(path.read_text() for path in ops[2].root.rglob('*.json'))


@pytest.mark.parametrize(('failure', 'gate'), [
    ('pytest', 'pytest'), ('leak', 'leak'), ('shape', 'release_shape'), ('sanity', 'sanity'),
    ('pre-upload', 'deploy'), ('drift', 'deploy'),
])
def test_failing_gates_keep_previous_release(ops, failure, gate):
    activate(ops)
    old = baseline(ops)
    batch(ops, 17000)
    fault(ops, failure)
    result = run(ops)
    assert result.returncode == 1 and gate in result.stderr
    assert baseline(ops) == old and '15000' in (ops[0] / 'saunas.csv').read_text()
    assert calls(ops).count('deploy') == int(gate == 'deploy')
    assert not (ops[3].parent / 'live').exists()
    log = (ops[1] / 'publish/publish.log').read_text().splitlines()
    assert len(log) == 2 and f'gate={gate}' in log[-1] and '+09:00 ' in log[-1]


def test_tampered_generation_and_request_are_terminal(ops):
    activate(ops)
    request = batch(ops, 17000)
    artifact = ops[2].generations / request['generation_digest'] / change_feeds.ARTIFACTS[0]
    artifact.write_bytes(artifact.read_bytes() + b' ')
    (ops[2].requests / ('0' * 64 + '.json')).write_text('{}')
    assert run(ops).returncode == 1 and not calls(ops)
    assert receipts(ops, request)[0]['reason'] == 'generation_invalid'
    assert receipts(ops, {'request_id': '0' * 64})[0]['reason'] == 'request_invalid'
    at = publisher.now()
    assert ops[2].terminal_attempts(at.replace(hour=0, minute=0, second=0, microsecond=0),
                                    at + timedelta(seconds=1)) == ['failed']
    assert run(ops).returncode == 0 and not calls(ops)


@pytest.mark.parametrize('resolution', ['live', 'previous', 'confirmation'])
def test_uncertain_upload_never_replays_until_reconciled(ops, resolution):
    activate(ops)
    request = batch(ops, 17000)
    old = baseline(ops)
    fault(ops, 'confirmed-crash' if resolution == 'confirmation' else 'uncertain')
    assert run(ops).returncode == 1 and baseline(ops) == old
    if resolution == 'confirmation':
        assert run(ops, '--recover').returncode == 0
    else:
        assert 'upload_outcome_uncertain' in run(ops).stderr
        path = ops[3] if resolution == 'previous' else ops[3].parent / 'live'
        assert run(ops, '--reconcile-live', path).returncode == 0
    assert calls(ops).count('deploy') == 1
    if resolution == 'previous':
        assert baseline(ops) == old
        assert {row['status'] for row in receipts(ops, request)} == {'pending', 'failed'}
    else:
        assert baseline(ops) != old and '17000' in (ops[0] / 'saunas.csv').read_text()
        assert {row['status'] for row in receipts(ops, request)} == {'pending', 'deployed'}
    assert not (ops[2].root / 'pending.json').exists()


def test_failed_readiness_invalidates_old_marker_and_preserves_state(ops):
    no_readiness = run(ops)
    assert no_readiness.returncode == 1 and 'readiness_required' in no_readiness.stderr
    activate(ops)
    old = baseline(ops)
    paths = [ops[0] / 'saunas.csv', ops[1] / 'ig/collector.sqlite3', ops[2].root / 'consent.json']
    old_bytes = [p.read_bytes() for p in paths]
    different = ops[3].parent / 'different'
    shutil.copytree(ops[3], different)
    (different / 'index.html').write_text('different artifact')
    assert run(ops, '--check-readiness', '--deployed-artifact', different).returncode == 1
    assert not ops[2].ready() and baseline(ops) == old
    assert [p.read_bytes() for p in paths] == old_bytes and not calls(ops)
    assert run(ops, '--check-readiness', '--deployed-artifact', different, '--rebaseline-baseline').returncode == 0
    assert ops[2].ready() and baseline(ops) != old


@pytest.mark.parametrize(('name', 'message'), [
    ('consent.json', 'consent'), ('site_slug_map.json', 'mapping'),
    ('ledger.json', 'ledger_invalid'), ('collector.sqlite3', 'backup_failed'),
])
def test_invalid_runtime_inputs_fail_closed(ops, name, message):
    activate(ops)
    batch(ops, 17000)
    path = (ops[1] / 'ig' if name in ('collector.sqlite3', 'site_slug_map.json') else ops[2].root) / name
    path.write_text('malformed runtime state')
    assert message in run(ops).stderr
    assert not calls(ops)


def test_lock_is_shared_across_processes(ops):
    activate(ops)
    batch(ops, 17000)
    with lock(ops[1] / 'publish/deploy.lock'):
        result = run(ops)
    assert result.returncode == 1 and 'already_running' in result.stderr and not calls(ops)
    assert run(ops).returncode == 0 and calls(ops).count('deploy') == 1


def test_backup_retains_unverified_set_and_captures_wal(tmp_path):
    invalid = tmp_path / 'backups/publish-20000101T000000000000+0900'
    invalid.mkdir(parents=True)
    publisher.non_publishing_check(tmp_path)
    assert invalid.is_dir()
    assert len(list(invalid.parent.iterdir())) == 15


def test_sanity_cutoffs_and_empty_build(tmp_path):
    def manifest(pages, size, venues=()):
        return {'artifacts': {'index.html': {'bytes': 1}}, 'page_count': pages,
                'total_bytes': size, 'venue_slugs': list(venues)}

    old = manifest(400, 100, [SLUG])
    rm.sanity(old, manifest(380, 50, [SLUG]), set())
    rm.sanity(old, manifest(401, 90, [SLUG]), set())
    rm.sanity(old, manifest(380, 50), {SLUG})
    for new, reason in [
        (manifest(379, 100, [SLUG]), 'page_count_drop'),
        (manifest(400, 49, [SLUG]), 'size_drop'),
        (manifest(400, 100), 'venue_removed_without_closure'),
        (rm.artifact_manifest(tmp_path, None), 'empty_build'),
    ]:
        with pytest.raises(rm.GateError, match=reason):
            rm.sanity(old, new, set())
    with pytest.raises(rm.GateError, match='page_count_drop'):
        rm.sanity(manifest(100, 100), manifest(94, 100), set())
    assert not rm.permanent_closure('Closed every Sunday')
    assert rm.permanent_closure('Permanently closed')


@pytest.mark.parametrize('value', ['Closed every Sunday', 'Permanently closed'])
def test_closure_inventory_changes_only_for_permanent_notice(ops, value):
    activate(ops)
    batch(ops, 'Open every day', field='closures')
    request = batch(ops, value, 2, field='closures')
    result = run(ops)
    assert result.returncode == 0, result.stderr
    assert any(r['status'] == 'deployed' for r in receipts(ops, request))
    inventory = rm.read_json(ops[0] / 'site/content/venues_en.json')
    assert (SLUG in inventory) == (value == 'Closed every Sunday')
    assert rm.read_json(ops[0] / 'site/content/closed_days.json')[SLUG]['closed'] == value
    if value == 'Permanently closed':
        batch(ops, 'Normally open', 3, field='closures')
        result = run(ops)
        assert result.returncode == 0, result.stderr
        assert SLUG in rm.read_json(ops[0] / 'site/content/venues_en.json')


def test_unconfirmed_and_independent_observations_do_not_create_transition(ops):
    activate(ops)
    assert batch(ops, 18000, confirmed=False) is None
    assert batch(ops, 18000, 2, source_type='independent') is None
    assert batch(ops, 18000, 3, source_type='third_party') is None
    assert run(ops).returncode == 0 and not calls(ops)


def test_declined_identity_name_alias_and_official_url(ops):
    assert rm.excluded_venue(CONSENT['venues'], ['RUNPORT'], [])
    assert rm.excluded_venue(CONSENT['venues'], ['Box B'], [])
    assert rm.excluded_venue(CONSENT['venues'], ['Seed Spa A'], ['https://declined.example.test/branch/a'])
    assert not rm.excluded_venue(CONSENT['venues'], ['Seed Spa A'], ['https://declined.example.test/other'])


def test_checkout_writeback_can_resume_without_another_upload(ops):
    activate(ops)
    batch(ops, 17000)
    csv = ops[0] / 'saunas.csv'
    original = csv.read_bytes()
    old = baseline(ops)
    fault(ops, 'checkout-interrupted')
    first = run(ops)
    assert first.returncode == 1 and 'checkout_sync_pending' in first.stderr
    assert baseline(ops) != old
    assert csv.read_text() == 'concurrent edit'
    assert 'checkout_sync_pending' in run(ops).stderr
    assert run(ops, '--reconcile-checkout').returncode == 1
    csv.write_bytes(original)
    assert run(ops, '--reconcile-checkout').returncode == 0
    assert '17000' in csv.read_text() and calls(ops).count('deploy') == 1


def test_managed_slot_detects_human_edit_before_deploy(ops):
    activate(ops)
    batch(ops, 17000)
    assert run(ops).returncode == 0
    csv = ops[0] / 'saunas.csv'
    csv.write_text(csv.read_text().replace('17000', '18500'))
    old = baseline(ops)
    batch(ops, 19000, 2)
    assert 'managed_slot_conflict' in run(ops).stderr
    assert baseline(ops) == old and csv.read_text().endswith('18500\n') and calls(ops).count('deploy') == 1


def test_regular_hours_project_with_citation(ops):
    activate(ops)
    assert batch(ops, {'hours': '09:00-18:00'}, field='hours') is None
    request = batch(ops, {'hours': '10:00-19:00'}, 2, field='hours')
    result = run(ops)
    assert result.returncode == 0, result.stderr
    assert any(row['status'] == 'deployed' for row in receipts(ops, request))
    record = rm.read_json(ops[0] / 'site/content/venues.json')[NAME]
    assert record['price_hours_evidence']['Hours'] == {
        'source_kind': 'official_page', 'source_url': URL, 'checked_on': DAY.date().isoformat(),
        'value': '10:00–19:00',
    }


def test_legacy_null_claim_provenance_survives_publish(ops):
    activate(ops)
    source = ops[1] / 'ig'
    batch(ops, 17000)
    db_path = source / 'collector.sqlite3'
    before = db_path.read_bytes()
    result = run(ops)
    assert result.returncode == 0, result.stderr
    assert db_path.read_bytes() == before
    with closing(publisher.readonly(db_path)) as db:
        assert tuple(db.execute('SELECT evidence_origin, evidence_role, verbatim_source FROM claims').fetchone()) == (None, None, None)
