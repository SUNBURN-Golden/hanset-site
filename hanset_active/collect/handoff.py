"""Durable review-export handoff to the external operations publisher."""
import json
import os
from pathlib import Path
import tempfile

from ..util.timeutil import KST
from .change_feeds import ARTIFACTS, manifest_status, sha
from .changes import HEX
from .private_io import directory, json_bytes, read, read_json, replace, sync_directory
from .promotion import timestamp

VERSION = 1
TERMINAL = {'deployed', 'failed'}
ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError)


def digest(value):
    return isinstance(value, str) and bool(HEX.fullmatch(value))


def kst_timestamp(value):
    parsed = timestamp(value)
    if parsed is None or parsed.utcoffset() != KST.utcoffset(None):
        raise ValueError('publication_timestamp_invalid')
    return parsed


class Outbox:
    def __init__(self, root):
        self.root = root / 'publication' / 'v1'
        self.generations = self.root / 'generations'
        self.requests = self.root / 'requests'
        self.receipts = self.root / 'receipts'

    def ready(self):
        try:
            row = read_json(self.root / 'readiness.json')
            return (type(row['schema_version']) is int and row['schema_version'] == VERSION
                    and isinstance(row['consumer_version'], str) and bool(row['consumer_version'].strip())
                    and row['non_publishing_check'] == 'passed'
                    and timestamp(row['confirmed_at']) is not None)
        except ERRORS:
            return False

    @staticmethod
    def request(slot, generation, ids, *, job='wide_collect'):
        slot = slot.astimezone(KST).isoformat()
        request_id = sha(json_bytes([job, slot, generation]))
        row = {'schema_version': VERSION, 'request_id': request_id, 'slot': slot,
               'generation_digest': generation, 'change_ids': ids}
        if job != 'wide_collect':
            row['job'] = job
        return row

    def prepare(self, export_dir, result, slot, *, job='wide_collect'):
        """Verify and freeze the export before recording collection completion."""
        if result.get('reason') == 'no_events':
            return None
        if result.get('reason') != 'ok':
            raise ValueError('export_incomplete')
        verified = manifest_status(export_dir, expected_watermark=result['event_watermark'])
        generation = verified.get('manifest_sha256')
        if verified['state'] != 'ok' or generation != result.get('manifest_sha256'):
            raise ValueError('export_invalid')
        self._stage_generation(export_dir / 'official-web-changes', generation)
        ids = self.verify_generation(generation)
        return self.request(slot, generation, ids, job=job) if ids else None

    def _stage_generation(self, source, generation):
        directory(self.generations)
        final = self.generations / generation
        if final.exists():
            sync_directory(self.generations)
            return
        with tempfile.TemporaryDirectory(dir=self.generations) as temporary:
            staging = Path(temporary)
            manifest_data = read(source / 'manifest.json')
            if sha(manifest_data) != generation:
                raise ValueError('manifest_changed')
            for entry in json.loads(manifest_data)['artifacts']:
                data = read(source / entry['name'])
                if len(data) != entry['bytes'] or sha(data) != entry['sha256']:
                    raise ValueError('artifact_invalid')
                replace(staging / entry['name'], data)
            replace(staging / 'manifest.json', manifest_data)
            os.rename(staging, final)
            sync_directory(self.generations)

    def verify_generation(self, generation):
        if not digest(generation):
            raise ValueError('manifest_invalid')
        final = self.generations / generation
        if final.is_symlink():
            raise ValueError('unsafe_generation')
        data = read(final / 'manifest.json')
        if sha(data) != generation:
            raise ValueError('manifest_invalid')
        manifest = json.loads(data)
        artifacts = manifest['artifacts']
        if (manifest['schema_version'] != VERSION or manifest['rights'] != 'internal_review_only'
                or manifest['auto_publish_to_site'] is not False
                or sorted(entry['name'] for entry in artifacts) != sorted(ARTIFACTS)):
            raise ValueError('manifest_invalid')
        contents = {}
        for entry in artifacts:
            data = read(final / entry['name'])
            if len(data) != entry['bytes'] or sha(data) != entry['sha256']:
                raise ValueError('artifact_invalid')
            contents[entry['name']] = data
        ids = {cid for row in json.loads(contents[ARTIFACTS[1]])['candidates']
               for cid in row['latest_change_ids']}
        if not all(map(digest, ids)):
            raise ValueError('change_id_invalid')
        return sorted(ids)

    def validate_request(self, request):
        slot = kst_timestamp(request['slot'])
        generation = request['generation_digest']
        ids = request['change_ids']
        job = request.get('job', 'wide_collect')
        if (job not in ('wide_collect', 'daily_collect') or not digest(generation) or not isinstance(ids, list) or not ids
                or not all(map(digest, ids)) or ids != sorted(set(ids))
                or request != self.request(slot, generation, ids, job=job)):
            raise ValueError('request_invalid')

    def expose(self, request):
        self.validate_request(request)
        if self.verify_generation(request['generation_digest']) != request['change_ids']:
            raise ValueError('request_generation_mismatch')
        path = self.requests / (request['request_id'] + '.json')
        data = json_bytes(request)
        # All writes to this outbox are made under their scheduled job lock; the request ID includes the job.
        try:
            existing = read(path)
        except FileNotFoundError:
            replace(path, data)
        else:
            if existing != data:
                raise ValueError('request_conflict')
            sync_directory(self.requests)

    def terminal_attempts(self, start, cutoff):
        """Read receipt records only. A missing or pending receipt has no count."""
        attempts = {}
        for path in sorted((self.receipts / start.astimezone(KST).date().isoformat()).glob('*/*.json')):
            row = read_json(path)
            status = row['status']
            if status in ('pending', 'no_change'):
                continue
            # A malformed request has no validated identity to bind to an attempt.
            if status == 'failed' and row.get('reason') == 'request_invalid':
                continue
            completed = timestamp(row.get('completed_at'))
            recorded = timestamp(row.get('recorded_at'))
            if status not in TERMINAL or completed is None or recorded is None or completed > recorded:
                raise ValueError('receipt_invalid')
            if not (start <= completed < cutoff and recorded < cutoff):
                continue
            request_id = row['request_id']
            if not digest(request_id) or request_id != path.parent.name:
                raise ValueError('receipt_invalid')
            request = read_json(self.requests / (request_id + '.json'))
            self.validate_request(request)
            attempt_id = row['deployment_attempt_id']
            if (row['schema_version'] != VERSION or request['request_id'] != request_id
                    or row['generation_digest'] != request['generation_digest']
                    or not isinstance(attempt_id, str) or not 0 < len(attempt_id) <= 200):
                raise ValueError('receipt_invalid')
            value = (status, completed)
            if attempts.setdefault(attempt_id, value) != value:
                raise ValueError('receipt_conflict')
        return [status for status, _ in attempts.values()]
