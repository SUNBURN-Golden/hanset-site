"""Atomic checkpoint and status. Schema 1 is the retired browser draft and is refused."""
import json
import os
import tempfile
from datetime import date
from pathlib import Path

from hanset_active.util.timeutil import Clock, kst_day, kst_now

from . import CollectError
from .config import MODEL_CALL_CAP, MODEL_INPUT_CAP, MODEL_OUTPUT_CAP, outside

SCHEMA = 2
HALT_REASONS = frozenset({'security_check', 'login_warning', 'challenge'})


def new_checkpoint(day):
    return {
        'schema_version': SCHEMA,
        'day': day,
        'counts': {'pages': 0, 'claims': 0, 'ig_reads': 0},
        'budgets': {'model_calls': 0, 'input_chars': 0, 'output_tokens': 0},
        'completed': {},
        'halt': None,
        'previous_halt': None,
    }


def atomic_json(path, value):
    path = outside(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    blob = (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode()
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _ints(value, keys):
    return isinstance(value, dict) and set(value) == set(keys) and all(type(value[key]) is int and value[key] >= 0 for key in keys)


def _validate(saved, today):
    if not isinstance(saved, dict) or saved.get('schema_version') != SCHEMA:
        raise ValueError
    if saved.get('day') != date.fromisoformat(saved['day']).isoformat():
        raise ValueError
    if saved['day'] > today:
        raise ValueError
    if not _ints(saved.get('counts'), ('pages', 'claims', 'ig_reads')):
        raise ValueError
    if not _ints(saved.get('budgets'), ('model_calls', 'input_chars', 'output_tokens')):
        raise ValueError
    completed = saved.get('completed')
    if not isinstance(completed, dict) or len(completed) > 100:
        raise ValueError
    if not all(isinstance(key, str) and len(key) <= 500 and isinstance(item, str) and len(item) <= 80 for key, item in completed.items()):
        raise ValueError
    halt = saved.get('halt')
    if halt is not None and (not isinstance(halt, dict) or set(halt) != {'day', 'reason'} or halt['day'] != saved['day'] or halt['reason'] not in HALT_REASONS):
        raise ValueError
    previous = saved.get('previous_halt')
    if previous is not None and (not isinstance(previous, dict) or set(previous) != {'day', 'reason'} or previous['reason'] not in HALT_REASONS):
        raise ValueError
    return saved


class State:
    def __init__(self, directory, clock=None):
        self.directory = outside(directory)
        self.clock = clock or Clock()
        self.path = self.directory / 'checkpoint.json'
        today = kst_day(self.clock)
        self.data = new_checkpoint(today)
        # Per-run diagnostics only; never part of checkpoint.json.
        self.model_providers = {}
        self.primary_skipped = None
        self.proxy_heal = None
        self.corrupt = False
        if not self.path.exists():
            return
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 1_000_000:
                raise ValueError
            saved = json.loads(self.path.read_bytes().decode())
            saved = _validate(saved, today)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            self.corrupt = True
            raise CollectError('state_corrupt') from None
        if saved['day'] == today:
            self.data = saved
        else:
            self.data = new_checkpoint(today)
            self.data['previous_halt'] = saved.get('halt') or saved.get('previous_halt')
            self.save()

    def save(self):
        if self.corrupt:
            raise CollectError('state_corrupt')
        atomic_json(self.path, self.data)

    def halt(self, reason):
        if reason not in HALT_REASONS:
            raise CollectError('runtime_error')
        self.data['halt'] = {'day': self.data['day'], 'reason': reason}
        self.save()

    def halted(self):
        halt = self.data.get('halt')
        return bool(halt) and halt.get('day') == self.data['day']

    def can_model(self, chars, output_tokens):
        budgets = self.data['budgets']
        return (budgets['model_calls'] < MODEL_CALL_CAP and
                budgets['input_chars'] + chars <= MODEL_INPUT_CAP and
                budgets['output_tokens'] + output_tokens <= MODEL_OUTPUT_CAP)

    def reserve_model(self, chars, output_tokens):
        if not self.can_model(chars, output_tokens):
            raise CollectError('model_budget_exhausted')
        budgets = self.data['budgets']
        budgets['model_calls'] += 1
        budgets['input_chars'] += chars
        budgets['output_tokens'] += output_tokens
        self.save()

    def record_provider(self, name):
        if name in ('deepseek', 'qwen'):
            self.model_providers[name] = self.model_providers.get(name, 0) + 1

    def reconcile_output(self, reserved, used):
        if type(used) is int and 0 <= used <= reserved:
            self.data['budgets']['output_tokens'] -= reserved - used
            self.save()

    def complete(self, url, disposition):
        self.data['completed'][url] = disposition
        self.save()

    def write_status(self, result):
        body = {
            'schema_version': SCHEMA,
            'state': result['state'],
            'reason': result['reason'],
            'success': bool(result['success']),
            'collected': bool(result['collected']),
            'mode': result['mode'],
            'at': kst_now(self.clock).isoformat(),
            'counts': self.data['counts'],
            'budgets': self.data['budgets'],
            'halt': self.data.get('halt'),
            'pages': result.get('pages') or [],
            'provider': result.get('provider') or {},
        }
        if 'proxy_heal' in result:
            body['proxy_heal'] = dict(result['proxy_heal'])
        if result.get('primary_skipped') == 'insufficient_balance':
            body['primary_skipped'] = 'insufficient_balance'
        atomic_json(self.directory / 'status.json', body)
