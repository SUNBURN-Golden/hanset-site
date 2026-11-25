"""Consume official web change requests and operate the managed release gates."""

import argparse
from contextlib import ExitStack, closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'site'), str(ROOT / 'scripts')]

from hanset_active.collect import change_feeds
from hanset_active.collect.handoff import ERRORS, Outbox, digest, kst_timestamp
from hanset_active.collect.private_io import Busy, directory, lock, read, read_json, sync_directory
from publish_inputs import PublicationInputs
from release_artifact import seal
from release_contract import CONTRACT, receipt_binding
import release_manifest as rm

KST = ZoneInfo('Asia/Seoul')
VERSION = 'collx-05:1'
BACKUP_SET = re.compile(r'publish-\d{8}T\d{12}\+0900')
DB_NAMES = ('active.sqlite3', 'twin.sqlite3', 'collector.sqlite3')
DISPOSITIONS = {'deployed', 'no_change', 'superseded'}
REQUEST_FAILURES = {'request_invalid', 'generation_invalid'}


def now():
    return datetime.now(KST)


def path_setting(name, default):
    path = Path(os.environ.get(name) or default)
    if not path.is_absolute() or '..' in path.parts:
        raise rm.GateError('config', 'absolute_path_required')
    return path


def safe_directory(path):
    if any(entry.is_symlink() for entry in (path, *path.parents)):
        raise ValueError('unsafe_path')
    return path


def log(path, gate, reason, detail=''):
    fd = os.open(path / 'publish.log', os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a', encoding='utf-8') as stream:
        stream.write(f'{now().isoformat(timespec="seconds")} gate={gate} reason={reason}{detail}\n')


def readonly(path):
    safe_directory(path)
    if not path.is_file():
        raise ValueError('db_missing')
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def file_info(path):
    with path.open('rb') as stream:
        return {'sha256': hashlib.file_digest(stream, 'sha256').hexdigest(),
                'bytes': os.fstat(stream.fileno()).st_size}


def db_backup(source, target):
    deadline = time.monotonic() + 60

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError

    with closing(readonly(source)) as original, closing(sqlite3.connect(target)) as copy:
        original.backup(copy, pages=256, progress=progress)
        if copy.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ValueError('backup_integrity')
    with target.open('rb') as stream:
        os.fsync(stream.fileno())


def backup(root, databases):
    directory(root)
    with tempfile.TemporaryDirectory(prefix='.partial-', dir=root) as temp:
        staged = Path(temp)
        manifest = {}
        for source in databases:
            target = staged / source.name
            db_backup(source, target)
            manifest[source.name] = file_info(target)
        rm.atomic_json(staged / 'manifest.json', manifest)
        destination = root / now().strftime('publish-%Y%m%dT%H%M%S%f%z')
        staged.rename(destination)
        sync_directory(root)
    complete = []
    for path in sorted(root.iterdir()):
        if not BACKUP_SET.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
            continue
        try:
            files = read_json(path / 'manifest.json')
            if (set(files) == set(DB_NAMES)
                    and {entry.name for entry in path.iterdir()} == set(DB_NAMES) | {'manifest.json'}
                    and all((path / name).is_file() and not (path / name).is_symlink()
                            and file_info(path / name) == files[name] for name in DB_NAMES)):
                complete.append(path)
        except ERRORS:
            continue
    for path in complete[:-14]:
        shutil.rmtree(path)
    sync_directory(root)
    return destination


def clean_env(temp):
    for name in ('home', 'tmp'):
        (temp / name).mkdir()
    return {'PATH': os.environ.get('PATH', os.defpath), 'LANG': 'C.UTF-8', 'TZ': 'Asia/Seoul',
            'HOME': str(temp / 'home'), 'TMPDIR': str(temp / 'tmp'),
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
            'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
            'HANSET_QWEN_KEY_FILE': '/dev/null', 'HANSET_DATAGO_KEY_FILE': '/dev/null'}


def snapshot(root, stage, env):
    try:
        safe_directory(root)
        result = subprocess.run(['git', '-C', str(root), 'ls-files', '--stage', '-z'], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
        for entry in result.stdout.split(b'\0'):
            if not entry:
                continue
            flags, name = entry.split(b'\t', 1)
            mode, _, index = flags.split()
            relative = Path(os.fsdecode(name))
            source = root / relative
            if (mode not in (b'100644', b'100755') or index != b'0' or relative.is_absolute()
                    or '..' in relative.parts or not safe_directory(source).is_file()):
                raise ValueError
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise rm.GateError('stage', 'stage_invalid') from None


def validate_mapping(ig):
    try:
        mapping = change_feeds._mapping(ig)
        if not all(venue_id.strip() and venue_key.strip() for venue_id, venue_key in mapping):
            raise ValueError
    except ERRORS:
        raise rm.GateError('mapping', 'mapping_invalid') from None
    if not mapping:
        raise rm.GateError('mapping', 'mapping_empty')
    return mapping


def require_consent(root, outbox, today):
    registry = rm.consent_registry(outbox.root / 'consent.json', today)
    try:
        queue = read_json(root / 'data/pipeline/queue.json')
        declined = queue['stages']['4_published_or_expired']['excluded_declined_venues']
        if not any(row['slug'] == 'runport-mapo' and '2026-10-19' in row['reason'] for row in declined):
            raise ValueError
    except ERRORS:
        raise rm.GateError('consent', 'consent_missing_or_invalid') from None
    return registry


def checked_command(command, root, env, *, pass_fds=(), capture=False):
    return subprocess.run([str(item) for item in command], cwd=root, env=env, pass_fds=pass_fds,
                          check=False, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                          stderr=subprocess.STDOUT if capture else subprocess.DEVNULL)


def gates(python, stage, dist, env, day, data_sha, previous, closed):
    build = checked_command([python, stage / 'site/build_site.py', '--release', '--out', dist,
                             '--today', day], stage, {**env, 'HANSET_RELEASE_DATE': day}, capture=True)
    if build.returncode:
        if b'LEAK-CHECK FAIL:' in build.stdout:
            raise rm.GateError('leak', 'emitted_output_rejected')
        raise rm.GateError('build', 'builder_failed')
    if checked_command([python, '-m', 'pytest', 'tests', '-q', '-p', 'no:cacheprovider'], stage, env).returncode:
        raise rm.GateError('pytest', 'suite_failed')
    manifest = seal(dist, data_sha)
    rm.sanity(previous, manifest, closed)
    return manifest


def verified_artifacts(outbox, request):
    gen = request['generation_digest']
    if outbox.verify_generation(gen) != request['change_ids']:
        raise ValueError
    root = outbox.generations / gen
    closed, rebuild, viral = change_feeds.ARTIFACTS
    manifest_data = read(root / 'manifest.json')
    if rm.digest(manifest_data) != gen:
        raise ValueError
    manifest = json.loads(manifest_data)
    contents = {}
    for entry in manifest['artifacts']:
        data = read(root / entry['name'])
        if len(data) != entry['bytes'] or rm.digest(data) != entry['sha256']:
            raise ValueError
        contents[entry['name']] = data
    for name in (closed, rebuild):
        contents[name] = json.loads(contents[name])
    topics = [json.loads(line) for line in contents[viral].splitlines()]
    if not all(type(row['schema_version']) is int and row['schema_version'] == 1
               and row['rights'] == 'internal_review_only' and row['auto_publish_to_site'] is False
               for row in (manifest, contents[closed], contents[rebuild], *topics)):
        raise ValueError
    candidates = {}
    for row in contents[rebuild]['candidates']:
        if (not isinstance(row['review_reasons'], list) or not isinstance(row['fields'], list)
                or not isinstance(row['latest_change_ids'], list)
                or not all(isinstance(row[key], str) and row[key] for key in ('venue_id', 'venue_key'))):
            raise ValueError
        for cid in row['latest_change_ids']:
            if not digest(cid) or cid in candidates:
                raise ValueError
            candidates[cid] = row
    if sorted(candidates) != request['change_ids']:
        raise ValueError
    proposals = contents[closed]['proposals']
    if not isinstance(proposals, list):
        raise ValueError
    for row in proposals:
        ids = row['change_ids']
        if (not isinstance(ids, list) or ids != sorted(set(ids))
                or not all(cid in candidates for cid in ids)):
            raise ValueError
    return candidates, proposals


def write_receipt(outbox, request, status, at, *, attempt=None, reason=None, release_id=None):
    record = {'schema_version': 1, 'request_id': request['request_id'],
              'generation_digest': request['generation_digest'], 'status': status,
              'completed_at': at, 'recorded_at': max(at, now().isoformat())}
    if status in ('deployed', 'failed'):
        record['deployment_attempt_id'] = attempt
    if reason:
        record['reason'] = reason
    if release_id:
        record['release_id'] = release_id
    folder = directory(outbox.receipts / kst_timestamp(at).date().isoformat() / request['request_id'])
    path = folder / f'{attempt or release_id or "batch"}-{status}.json'
    if not path.exists():
        rm.atomic_json(path, record)


def validate_ledger(row):
    try:
        if type(row['schema_version']) is not int or row['schema_version'] != 1 or set(row) != {'schema_version', 'changes'}:
            raise ValueError
        for cid, item in row['changes'].items():
            if (not digest(cid) or set(item) != {'generation', 'request_id', 'disposition', 'recorded_at'}
                    or not digest(item['request_id']) or not digest(item['generation'])
                    or item['disposition'] not in DISPOSITIONS):
                raise ValueError
            kst_timestamp(item['recorded_at'])
    except ERRORS:
        raise rm.GateError('ledger', 'ledger_invalid') from None
    return row


def make_ledger(ledger, requests, applied, superseded, status, at):
    entries = dict(ledger['changes'])
    for request in requests:
        for cid in request['change_ids']:
            entries.setdefault(cid, {
                'generation': request['generation_digest'], 'request_id': request['request_id'],
                'disposition': 'superseded' if cid in superseded else status if cid in applied else 'no_change',
                'recorded_at': at,
            })
    return {'schema_version': 1, 'changes': entries}


class Publication:
    def __init__(self, data, root):
        self.root, self.data = root, data
        self.outbox = Outbox(data)
        self.path = self.outbox.root
        self.ig = path_setting('HANSET_IG_DIR', data / 'ig')
        self.active = path_setting('HANSET_ACTIVE_DIR', data / 'active')
        self.twin = path_setting('HANSET_TWIN_DIR', data / 'twin')
        for path in (data, self.ig, self.active, self.twin):
            if path == root or root in path.parents or path == ROOT or ROOT in path.parents:
                raise rm.GateError('config', 'runtime_inside_checkout')
            safe_directory(path)
        self.logdir = directory(data / 'publish')
        self.lockfile = self.logdir / 'deploy.lock'
        self.pointer = self.path / 'last-deployed.json'
        self.pending_path = self.path / 'pending.json'
        self.ledger_path = self.path / 'ledger.json'

    def deploy_command(self, *, readiness=False):
        command = path_setting('HANSET_DEPLOY_CMD', '/workspace/fitness-hub/ops/deploy.sh')
        try:
            marker = command.with_name(command.name + '.hanset-contract')
            if not command.is_file() or not os.access(command, os.X_OK) or read(marker).strip() != CONTRACT.encode():
                raise ValueError
            if readiness:
                launcher = path_setting('HANSET_DEPLOY_LAUNCHER', '/workspace/fitness-hub/ops/deploy_locked.sh')
                if not launcher.is_file() or not os.access(launcher, os.X_OK):
                    raise ValueError
        except ERRORS:
            raise rm.GateError('deploy', 'deploy_contract_unavailable') from None
        return command

    def release_python(self):
        python = path_setting('HANSET_RELEASE_PYTHON', self.root / 'site/.venv/bin/python')
        if not python.is_file() or not os.access(python, os.X_OK):
            raise rm.GateError('build', 'release_python_unavailable')
        return python

    def read_release(self, release_id):
        if not digest(release_id):
            raise rm.GateError('baseline', 'missing_or_corrupt')
        try:
            path = safe_directory(self.path / 'releases' / release_id)
            record = read_json(path / 'release.json')
            if rm.digest(rm.canonical(record)) != release_id or record['schema_version'] != 1:
                raise ValueError
            kst_timestamp(record['created_at'])
            if not isinstance(record['managed_slots'], list):
                raise ValueError
            for name, revision in record['inputs'].items():
                if (name not in rm.PROJECTED_FILES or not digest(revision['before'])
                        or rm.digest(revision['text'].encode()) != revision['after']):
                    raise ValueError
            validate_ledger(record['ledger'])
            rm.validate_manifest(record['manifest'])
        except ERRORS:
            raise rm.GateError('baseline', 'missing_or_corrupt') from None
        return record

    def baseline(self, *, optional=False):
        try:
            pointer = read_json(self.pointer)
        except FileNotFoundError:
            if optional:
                return None, None
            raise rm.GateError('baseline', 'missing_or_corrupt') from None
        except ERRORS:
            raise rm.GateError('baseline', 'missing_or_corrupt') from None
        if not isinstance(pointer, dict) or pointer.get('schema_version') != 1:
            raise rm.GateError('baseline', 'missing_or_corrupt')
        release_id = pointer.get('release_id')
        return release_id, self.read_release(release_id)

    def ledger(self, baseline=None):
        try:
            ledger = validate_ledger(read_json(self.ledger_path))
        except FileNotFoundError:
            if baseline is not None and baseline['ledger']['changes']:
                raise rm.GateError('ledger', 'ledger_invalid') from None
            ledger = {'schema_version': 1, 'changes': {}}
        except ERRORS:
            raise rm.GateError('ledger', 'ledger_invalid') from None
        if baseline and any(ledger['changes'].get(cid) != slot for cid, slot in baseline['ledger']['changes'].items()):
            raise rm.GateError('ledger', 'ledger_invalid')
        return ledger

    def save_release(self, record):
        release_id = rm.digest(rm.canonical(record))
        folder = directory(self.path / 'releases' / release_id)
        rm.atomic_json(folder / 'release.json', record)
        return release_id

    def set_baseline(self, release_id):
        rm.atomic_json(self.pointer, {'schema_version': 1, 'release_id': release_id})

    def read_pending(self):
        try:
            value = read_json(self.pending_path)
        except FileNotFoundError:
            return None
        if (value['schema_version'] != 1 or not digest(value['release_id'])
                or not digest(value['previous_id']) or not re.fullmatch('[0-9a-f]{32}', value['attempt'])
                or not isinstance(value['requests'], list) or not value['requests']
                or value['phase'] not in ('waiting', 'confirmed', 'failed')):
            raise rm.GateError('pending', 'pending_invalid')
        for request in value['requests']:
            self.outbox.validate_request(request)
        if value['phase'] != 'waiting':
            kst_timestamp(value['completed_at'])
        if value['phase'] == 'failed' and value['reason'] not in ('deploy_failed_before_upload', 'reconciled_previous'):
            raise rm.GateError('pending', 'pending_invalid')
        return value

    def clear_pending(self):
        self.pending_path.unlink()
        sync_directory(self.path)

    def checkout(self, record, *, write=False):
        try:
            for name, revision in record['inputs'].items():
                path = safe_directory(self.root / name)
                current = rm.digest(read(path))
                if current == revision['after']:
                    continue
                if current != revision['before']:
                    raise ValueError
                if write:
                    rm.atomic_write(path, revision['text'].encode())
        except ERRORS:
            raise rm.GateError('checkout', 'checkout_sync_pending' if write else 'managed_slot_conflict') from None

    def confirmation(self, pending, record):
        folder = self.path / 'attempts' / pending['attempt']
        binding = receipt_binding(pending['release_id'], record['manifest'])
        marker, receipt = folder / 'upload-started.json', folder / 'confirmed.json'
        if not marker.exists() and not receipt.exists():
            return 'pre_upload'
        try:
            if (read_json(marker) == {**binding, 'outcome': 'upload_started'}
                    and read_json(receipt) == {**binding, 'outcome': 'confirmed'}):
                return 'confirmed'
        except ERRORS:
            pass
        return 'uncertain'

    def finish(self, pending, record, *, reconcile_checkout=False):
        if pending['phase'] != 'confirmed':
            pending = {**pending, 'phase': 'confirmed', 'completed_at': now().isoformat()}
            rm.atomic_json(self.pending_path, pending)
        elif not reconcile_checkout:
            raise rm.GateError('checkout', 'checkout_sync_pending')
        release_id, completion = pending['release_id'], pending['completed_at']
        current, _ = self.baseline()
        if current not in (release_id, pending['previous_id']):
            raise rm.GateError('baseline', 'missing_or_corrupt')
        # The pointer selects the release record, including its committed ledger
        # snapshot. Pending makes updating the ledger mirror and checkout resumable.
        self.set_baseline(release_id)
        rm.atomic_json(self.ledger_path, record['ledger'])
        for request in pending['requests']:
            write_receipt(self.outbox, request, 'deployed', completion,
                          attempt=pending['attempt'], release_id=release_id)
        self.checkout(record, write=True)
        self.clear_pending()

    def fail_pending(self, pending, reason):
        if pending['phase'] != 'failed':
            pending = {**pending, 'phase': 'failed', 'reason': reason, 'completed_at': now().isoformat()}
            rm.atomic_json(self.pending_path, pending)
        for request in pending['requests']:
            write_receipt(self.outbox, request, 'failed', pending['completed_at'],
                          attempt=pending['attempt'], reason=pending['reason'])
        self.clear_pending()

    def recover(self, *, live=None, checkout=False):
        pending = self.read_pending()
        if pending is None:
            if live is not None or checkout:
                raise rm.GateError('pending', 'no_pending_release')
            return False
        record = self.read_release(pending['release_id'])
        if pending['phase'] == 'confirmed':
            if not checkout:
                raise rm.GateError('checkout', 'checkout_sync_pending')
            self.finish(pending, record, reconcile_checkout=True)
        elif checkout:
            raise rm.GateError('pending', 'upload_outcome_uncertain')
        elif pending['phase'] == 'failed':
            self.fail_pending(pending, pending['reason'])
        elif (outcome := self.confirmation(pending, record)) == 'confirmed':
            self.finish(pending, record)
        elif live is not None:
            previous = self.read_release(pending['previous_id'])
            manifest = rm.artifact_manifest(live, None)
            rm.require_home(manifest)
            live_files = manifest['artifacts']
            candidate_matches = live_files == record['manifest']['artifacts']
            previous_matches = live_files == previous['manifest']['artifacts']
            if candidate_matches and not previous_matches:
                self.finish(pending, record)
            elif previous_matches and not candidate_matches:
                self.fail_pending(pending, 'reconciled_previous')
            else:
                raise rm.GateError('deploy', 'live_artifact_does_not_resolve_upload')
        elif outcome == 'pre_upload':
            self.fail_pending(pending, 'deploy_failed_before_upload')
        else:
            raise rm.GateError('deploy', 'upload_outcome_uncertain')
        return True

    def requests(self, only=None):
        requests, candidates, proposals = [], {}, []
        receipt_paths = {}
        for path in self.outbox.receipts.glob('*/*/*.json'):
            receipt_paths.setdefault(path.parent.name, []).append(path)
        if only is not None and not (self.outbox.requests / (only + '.json')).is_file():
            raise rm.GateError('request', 'request_invalid')
        invalid = None
        for path in sorted(self.outbox.requests.glob('*.json')):
            if only is not None and path.stem != only:
                continue
            rid = path.stem if digest(path.stem) else rm.digest(os.fsencode(path.name))
            try:
                records = [read_json(p) for p in receipt_paths.get(rid, ())]
                if any(item['status'] in ('deployed', 'no_change') or item.get('reason') in REQUEST_FAILURES
                       for item in records):
                    continue
            except ERRORS:
                raise rm.GateError('receipt', 'receipt_invalid') from None
            reference = {'request_id': rid, 'generation_digest': '0' * 64}
            try:
                request = read_json(path)
                if digest(request.get('generation_digest')):
                    reference['generation_digest'] = request['generation_digest']
                self.outbox.validate_request(request)
                if rid != request['request_id'] or path.stem != rid:
                    raise ValueError
            except (*ERRORS, RecursionError):
                invalid = invalid or ('request', 'request_invalid')
                write_receipt(self.outbox, reference, 'failed', now().isoformat(),
                              attempt=uuid4().hex, reason='request_invalid')
                continue
            try:
                entries, closures = verified_artifacts(self.outbox, request)
            except (*ERRORS, RecursionError):
                invalid = invalid or ('generation', 'generation_invalid')
                write_receipt(self.outbox, request, 'failed', now().isoformat(),
                              attempt=uuid4().hex, reason='generation_invalid')
                continue
            requests.append(request)
            for cid, entry in entries.items():
                if cid in candidates:
                    earlier = candidates[cid]
                    if any(earlier[key] != entry[key] for key in ('venue_id', 'venue_key')):
                        raise rm.GateError('generation', 'generation_invalid')
                    entry = {**entry, 'review_reasons': sorted(set(earlier['review_reasons'] + entry['review_reasons']))}
                candidates[cid] = entry
            proposals.extend(closures)
        if invalid:
            raise rm.GateError(*invalid)
        return requests, candidates, proposals

    def publish(self, fd, only=None):
        self.recover()
        if not self.outbox.ready():
            raise rm.GateError('readiness', 'readiness_required')
        previous_id, previous = self.baseline()
        ledger = self.ledger(previous)
        requests, candidates, proposals = self.requests(only)
        if not requests:
            log(self.logdir, 'publish', 'no_change')
            return
        deploy = self.deploy_command()
        stamp = now()
        today = stamp.date()
        python = self.release_python()
        registry = require_consent(self.root, self.outbox, today)
        mapping = validate_mapping(self.ig)
        candidates = {cid: row for cid, row in candidates.items() if cid not in ledger['changes']}
        with tempfile.TemporaryDirectory(prefix='stage-', dir=self.logdir) as temporary:
            temp = Path(temporary)
            stage, dist = temp / 'checkout', temp / 'candidate'
            env = clean_env(temp)
            snapshot(self.root, stage, env)
            paths = (self.active / DB_NAMES[0], self.twin / DB_NAMES[1], self.ig / DB_NAMES[2])
            try:
                with ExitStack() as stack:
                    for lockpath in (self.data / 'cron.lock', self.ig / 'cron.lock', self.ig / 'daemon.lock',
                                     self.active / 'run.lock', self.twin / 'daemon.lock'):
                        stack.enter_context(lock(lockpath))
                    try:
                        backup(self.data / 'backups', paths)
                    except (OSError, sqlite3.Error, ValueError):
                        raise rm.GateError('backup', 'backup_failed') from None
                    db = stack.enter_context(closing(readonly(paths[-1])))
                    db.execute('BEGIN')
                    inputs = PublicationInputs(stage)
                    slots, closed, applied, superseded, notes = inputs.project(
                        db, candidates, proposals, mapping, previous['managed_slots'], registry, today)
                    journal = inputs.materialize()
            except Busy:
                raise rm.GateError('backup', 'collector_busy') from None
            except (OSError, sqlite3.Error):
                raise rm.GateError('apply', 'projection_failed') from None
            data_sha = rm.data_hash(stage)
            old_hash = previous['manifest']['applied_data_sha256']
            if data_sha == old_hash or (old_hash is None and not journal):
                at = now().isoformat()
                rm.atomic_json(self.ledger_path, make_ledger(ledger, requests, applied, superseded, 'no_change', at))
                for request in requests:
                    write_receipt(self.outbox, request, 'no_change', at)
                log(self.logdir, 'publish', 'no_change')
                self.log_projection(notes)
                return

            manifest = gates(python, stage, dist, env, today.isoformat(), data_sha, previous['manifest'], closed)
            record = {'schema_version': 1, 'manifest': manifest, 'managed_slots': slots,
                      'inputs': journal, 'created_at': stamp.isoformat(),
                      'ledger': make_ledger(ledger, requests, applied, superseded, 'deployed', stamp.isoformat())}
            self.checkout(record)
            self.upload(fd, deploy, python, stage, env, requests, record, previous_id, today.isoformat())
            self.log_projection(notes)

    def log_projection(self, notes):
        for reason, cid in sorted(notes):
            log(self.logdir, 'projection', reason, f' change_id={cid}')

    def upload(self, fd, deploy, python, stage, env, requests, record, previous_id, day):
        release_id = self.save_release(record)
        attempt = uuid4().hex
        folder = directory(self.path / 'attempts' / attempt)
        expected = folder / 'expected.json'
        rm.atomic_json(expected, record['manifest'])
        pending = {'schema_version': 1, 'release_id': release_id, 'previous_id': previous_id,
                   'attempt': attempt, 'requests': requests, 'phase': 'waiting'}
        rm.atomic_json(self.pending_path, pending)
        at = now().isoformat()
        for request in requests:
            write_receipt(self.outbox, request, 'pending', at, release_id=release_id)
        # The operations command owns the upload and any authentication. It
        # inherits a sanitized environment and receives the open flock only.
        deploy_env = {**env, 'HANSET_SITE_ROOT': str(stage), 'HANSET_RELEASE_PYTHON': str(python),
                      'HANSET_RELEASE_DATE': day, 'HANSET_PUBLISH_EXPECTED': str(expected),
                      'HANSET_PUBLISH_RELEASE_ID': release_id,
                      'HANSET_PUBLISH_RECEIPT': str(folder / 'confirmed.json'),
                      'HANSET_PUBLISH_UPLOAD_STARTED': str(folder / 'upload-started.json'),
                      'HANSET_PUBLISH_LOCK_FILE': str(self.lockfile), 'HANSET_PUBLISH_LOCK_FD': str(fd)}
        try:
            result = checked_command([deploy], stage, deploy_env, pass_fds=(fd,))
            success = result.returncode == 0
        except OSError:
            success = False
        outcome = self.confirmation(pending, record)
        if success and outcome == 'confirmed':
            self.finish(pending, record)
            log(self.logdir, 'publish', 'deployed')
        elif outcome == 'pre_upload':
            self.fail_pending(pending, 'deploy_failed_before_upload')
            raise rm.GateError('deploy', 'deploy_failed_before_upload')
        elif outcome == 'confirmed':
            raise rm.GateError('deploy', 'confirmed_release_recovery_required')
        else:
            raise rm.GateError('deploy', 'upload_outcome_uncertain')

    def readiness(self, artifact, rebaseline):
        ready_path = directory(self.path) / 'readiness.json'
        ready_path.unlink(missing_ok=True)
        sync_directory(self.path)
        if self.read_pending() is not None:
            raise rm.GateError('readiness', 'pending_release_requires_recovery')
        self.deploy_command(readiness=True)
        self.release_python()
        validate_mapping(self.ig)
        today = now().date()
        require_consent(self.root, self.outbox, today)
        current_id, current = self.baseline(optional=True)
        ledger = self.ledger(current)
        manifest = rm.artifact_manifest(safe_directory(artifact), None)
        rm.require_home(manifest)
        same = current is not None and current['manifest']['artifacts'] == manifest['artifacts']
        if current and not same and not rebaseline:
            raise rm.GateError('baseline', 'rebaseline_required')
        with tempfile.TemporaryDirectory(prefix='readiness-', dir=self.logdir) as temporary:
            non_publishing_check(Path(temporary))
        if not same:
            record = {'schema_version': 1, 'manifest': manifest, 'managed_slots': [], 'inputs': {},
                      'created_at': now().isoformat(), 'ledger': ledger}
            if current_id is not None:
                record['managed_slots'] = current['managed_slots']
            self.set_baseline(self.save_release(record))
        rm.atomic_json(ready_path, {'schema_version': 1, 'consumer_version': VERSION,
                                    'non_publishing_check': 'passed', 'confirmed_at': now().isoformat()})
        if not self.outbox.ready():
            ready_path.unlink(missing_ok=True)
            sync_directory(self.path)
            raise rm.GateError('readiness', 'readiness_required')
        log(self.logdir, 'readiness', 'passed')


def synthetic_observation(db, target, at, value, *, confirmed=True, field='price_day_pass', source_type='own_site'):
    from hanset_active.collect import changes
    from hanset_active.collect.targets import source_authorization

    url = target['urls'][0]
    fingerprint = rm.digest(rm.canonical(value))
    row = {**target, 'url': url, 'final_url': url, 'seen_at': at.isoformat(), 'content_hash': fingerprint,
           'source_authorization': source_authorization(target, url)}
    claim = {**target, 'field': field, 'value': value,
             'quote': value if isinstance(value, str) else rm.canonical(value).decode(),
             'claim_id': fingerprint, 'source_url': url, 'source_kind': 'web', 'source_type': source_type,
             'status': 'source_claim', 'rights': 'internal_review_only', 'evidence_origin': 'web_verbatim',
             'evidence_role': 'page_span', 'verbatim_source': 1, 'model_confirmed': confirmed}
    observation = changes.insert_observation(db, row, [claim])
    changes.process_pending(db, [target])
    return observation


def self_check(condition):
    if not condition:
        raise rm.GateError('readiness', 'non_publishing_check_failed')


def non_publishing_check(root):
    from datetime import timedelta

    from hanset_active.collect.store import connect

    start = datetime(2026, 11, 20, 4, 15, tzinfo=KST)
    slug, name, url = 'seed-spa-a', 'Seed Spa A', 'https://example.test/hours'
    target = {'venue_id': 'venue-a', 'venue_key': slug, 'urls': [url], 'domains': {'example.test'}}
    ig = directory(root / 'ig')
    outbox = Outbox(root)
    rm.atomic_json(ig / 'site_slug_map.json', {'venue-a': {'venue_key': slug, 'site_slug': slug}})

    with closing(connect(ig)) as db:
        pass_value = {'ticket': 'day_pass', 'amount': 15000}
        self_check(synthetic_observation(db, target, start, pass_value) is not None)
        self_check(synthetic_observation(db, target, start + timedelta(days=1),
                                         {**pass_value, 'amount': 17000}) is not None)
        unconfirmed = synthetic_observation(db, target, start + timedelta(days=2),
                                            {**pass_value, 'amount': 19000}, confirmed=False)
        self_check(db.execute('SELECT values_json FROM web_change_observations WHERE observation_id=?',
                              (unconfirmed,)).fetchone()[0] == '[]')
        export = change_feeds.export_changes(db, ig)
        request = outbox.prepare(ig, export, start, job='daily_collect')
        outbox.expose(request)
        self_check(len(request['change_ids']) == 1)
        candidates, proposals = verified_artifacts(outbox, request)
        artifact = outbox.generations / request['generation_digest'] / change_feeds.ARTIFACTS[1]
        content = read(artifact)
        rm.atomic_write(artifact, content + b' ')
        try:
            verified_artifacts(outbox, request)
        except ValueError:
            pass
        else:
            self_check(False)
        rm.atomic_write(artifact, content)

        stage = root / 'stage'
        content_dir = directory(stage / 'site/content')
        row = {'title': name, 'slug': slug, 'csv': 'saunas', 'facts': [['요금', '{price}']]}
        rm.atomic_json(content_dir / 'venues.json', {name: row})
        rm.atomic_json(content_dir / 'venues_en.json', {slug: {**row, 'facts': [['Price', '{price}']]}})
        rm.atomic_json(content_dir / 'closed_days.json', {})
        (stage / 'saunas.csv').write_text(f'name,website,price_krw\n{name},{url},15000\n')
        (stage / 'boxes.csv').write_text('name,website,drop_in_price_krw\n')
        inputs = PublicationInputs(stage)
        with closing(readonly(ig / DB_NAMES[2])) as source:
            _, _, applied, _, _ = inputs.project(source, candidates, proposals, validate_mapping(ig), [], [], start.date() + timedelta(days=2))
        self_check(applied == set(request['change_ids']) and 'saunas.csv' in inputs.materialize())
        self_check('17000' in (stage / 'saunas.csv').read_text())

    databases = [root / DB_NAMES[0], root / DB_NAMES[1], ig / DB_NAMES[2]]
    with closing(sqlite3.connect(databases[0])) as active, closing(sqlite3.connect(databases[1])) as twin:
        active.execute('PRAGMA journal_mode=WAL')
        active.execute('PRAGMA wal_autocheckpoint=0')
        active.execute('CREATE TABLE sample (value INTEGER)')
        active.execute('INSERT INTO sample VALUES (42)')
        active.commit()
        twin.execute('CREATE TABLE sample (value INTEGER)')
        twin.commit()
        self_check(databases[0].with_suffix('.sqlite3-wal').stat().st_size > 0)
        copies = [backup(root / 'backups', databases) for _ in range(15)]
        self_check(not copies[0].exists() and all(path.exists() for path in copies[1:]))
        with closing(readonly(copies[-1] / DB_NAMES[0])) as copy:
            self_check(copy.execute('SELECT value FROM sample').fetchone()[0] == 42)
    completed = now()
    write_receipt(outbox, request, 'deployed', completed.isoformat(), attempt=uuid4().hex)
    cutoff = now() + timedelta(seconds=1)
    self_check(outbox.terminal_attempts(completed.replace(hour=0, minute=0, second=0, microsecond=0),
                                        cutoff) == ['deployed'])


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--drain', action='store_true')
    modes.add_argument('--request')
    modes.add_argument('--check-readiness', action='store_true')
    modes.add_argument('--recover', action='store_true')
    modes.add_argument('--reconcile-live', type=Path)
    modes.add_argument('--reconcile-checkout', action='store_true')
    modes.add_argument('--status', action='store_true')
    parser.add_argument('--deployed-artifact', type=Path)
    parser.add_argument('--rebaseline-baseline', action='store_true')
    args = parser.parse_args(argv)
    if (bool(args.deployed_artifact) != args.check_readiness
            or (args.rebaseline_baseline and not args.check_readiness)
            or (args.request is not None and not digest(args.request))):
        parser.error('invalid publication arguments')
    publication = None
    try:
        data = path_setting('HANSET_DATA_DIR', '/workspace/hanset-data')
        root = path_setting('HANSET_SITE_ROOT', '/workspace/hanset-site')
        publication = Publication(data, root)
        with lock(publication.lockfile) as fd:
            if args.check_readiness:
                publication.readiness(args.deployed_artifact.absolute(), args.rebaseline_baseline)
                print('publication readiness passed')
            elif args.recover or args.reconcile_live or args.reconcile_checkout:
                publication.recover(live=args.reconcile_live, checkout=args.reconcile_checkout)
            elif args.status:
                print('readiness=' + ('ready' if publication.outbox.ready() else 'required'))
                print('pending=' + ('yes' if publication.read_pending() else 'no'))
                print('baseline=' + ('present' if publication.baseline(optional=True)[0] else 'missing'))
            else:
                publication.publish(fd, args.request)
        return 0
    except rm.GateError as error:
        gate, reason = error.gate, error.reason
    except Busy:
        gate, reason = 'lock', 'already_running'
    except (*ERRORS, sqlite3.Error, subprocess.SubprocessError, RecursionError, KeyboardInterrupt):
        gate, reason = 'publication', 'operation_failed'
    if publication is not None:
        try:
            log(publication.logdir, gate, reason)
        except OSError:
            pass
    print(f'{gate} {reason}', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
