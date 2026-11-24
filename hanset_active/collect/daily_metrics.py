"""Read-only daily observations and durable, idempotent KST metrics lines."""
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
import hashlib
import json
import os
import sqlite3
import sys
from time import monotonic

from ..network_store import AUDIT_FILE
from ..search.budget import BudgetUnavailable, _read as read_tavily
from ..util.timeutil import KST, kst_now
from . import CollectError
from .handoff import Outbox, kst_timestamp
from .plan import assemble_targets, identity
from .private_io import Busy, json_bytes, lock, read, read_json, replace, sync_directory
from .promotion import assess
from .promotion_policy import load_policy
from .store import read_claims
from .targets import source_authorization

CLOSE = time(23, 30)
PROVIDERS = ('deepseek', 'qwen')
AGGREGATES = {'seeds_total', 'targets', 'facts_total', 'unverified_total'}
COUNTS = AGGREGATES | {'seeds_new', 'pages_fetched', 'claims_new', 'deploys', 'deploy_failures', 'tavily_used'}
KEYS = COUNTS | {'date', 'provider_calls'}
SOURCE_ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, CollectError, BudgetUnavailable)


class TavilyCutoffUnknown(ValueError):
    pass


def cutoff(day):
    return datetime.combine(day, CLOSE, KST)


@contextmanager
def ledger(path):
    try:
        path.stat()
    except FileNotFoundError:
        yield None
        return
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.25)
    try:
        deadline = monotonic() + 3
        db.set_progress_handler(lambda: monotonic() >= deadline, 1000)
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        yield db
    finally:
        db.close()


def has_table(db, table):
    return db is not None and db.execute(
        'SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('table', table)).fetchone() is not None


def fetched_pages(db, bounds):
    if has_table(db, 'request_log') and any(
            row['name'] == 'transfer_complete' for row in db.execute('PRAGMA table_info(request_log)')):
        return db.execute('''SELECT count(*) FROM request_log WHERE ts>=? AND ts<?
            AND kind='body' AND status BETWEEN 200 AND 299 AND bytes>0 AND transfer_complete=1''', bounds).fetchone()[0]
    return 0


def counts_valid(row, keys):
    return all(type(row[key]) is int and row[key] >= 0 for key in keys)


class DailyMetrics:
    def __init__(self, root, settings, clock, *, testing=False):
        self.directory = root / 'metrics'
        self.settings = settings
        self.clock = clock
        self.testing = testing
        self.outbox = Outbox(root)
        self.output = self.directory / 'collect-daily.jsonl'
        self.journal = self.directory / 'append.json'
        self.initialized = self.directory / 'initialized.json'
        self.observations = self.directory / 'observations'
        self.guard = self.directory / 'daily.lock'
        self.active_path = settings.active_dir / 'active.sqlite3'
        self.claim_path = settings.data_dir / 'collector.sqlite3'
        self.model_path = settings.active_dir / 'collect-runtime.sqlite3'

    def run(self):
        try:
            with lock(self.guard):
                self._run()
        except Busy:
            pass
        except SOURCE_ERRORS:
            print('metrics_state_unreadable', file=sys.stderr)

    def _run(self):
        now = kst_now(self.clock)
        today = now.date()
        try:
            first = date.fromisoformat(read_json(self.initialized)['date'])
        except FileNotFoundError:
            if now >= cutoff(today):
                return
            first = today
            replace(self.initialized, json_bytes({'date': first.isoformat()}))

        self._recover_append()
        try:
            current = read(self.output)
        except FileNotFoundError:
            current = b''
        if current and not current.endswith(b'\n'):
            raise ValueError('metrics_output_invalid')
        closed = set()
        for line in current.splitlines():
            row = json.loads(line)
            if not self._valid_line(row) or row['date'] in closed:
                raise ValueError('metrics_output_invalid')
            closed.add(row['date'])

        offset, prefix = len(current), hashlib.sha256(current)
        day = first
        while cutoff(day) <= now:
            if day.isoformat() not in closed:
                try:
                    data = json_bytes(self._report(day, first))
                except TavilyCutoffUnknown:
                    print(f'metrics_tavily_cutoff_unknown {day.isoformat()}', file=sys.stderr)
                except SOURCE_ERRORS:
                    print(f'metrics_date_unavailable {day.isoformat()}', file=sys.stderr)
                else:
                    replace(self.journal, json_bytes({'offset': offset, 'line': data.decode(),
                                                      'prefix_sha256': prefix.hexdigest()}))
                    self._recover_append()
                    offset += len(data)
                    prefix.update(data)
            day += timedelta(days=1)

        if kst_now(self.clock) < cutoff(today):
            try:
                self._capture(today)
            except SOURCE_ERRORS:
                # Record a failed observation only if a valid observation never landed.
                path = self._path(today)
                if not path.exists():
                    replace(path, json_bytes({'unavailable': True}))
                print(f'metrics_observation_unavailable {today.isoformat()}', file=sys.stderr)

    def _path(self, day):
        return self.observations / f'{day.isoformat()}.json'

    def _tavily(self, day):
        days = read_tavily(self.settings.data_dir / 'tavily_usage.json')['budgets']['tavily']['days']
        return days.get(day.isoformat(), {}).get('used', 0)

    def _capture(self, day):
        now = kst_now(self.clock)
        options = {'now': now, 'testing': self.testing, 'include_watchlist': False, 'bounded': True}
        assembled = assemble_targets(self.settings, **options)
        if assembled['targets_status'] == 'invalid' or assembled['db_status'] == 'unreadable':
            raise ValueError('authorization_unreadable')
        targets = {identity(row): row for row in assembled['authorization']}
        policy_path = os.environ.get('HANSET_COLLECT_PROMOTION_POLICY')
        if not policy_path:
            path = self.settings.data_dir / 'promotion_policy.json'
            policy_path = path if path.exists() or path.is_symlink() else None
        policy = load_policy(policy_path, today=day)
        with ledger(self.active_path) as active:
            seeds = (active.execute('SELECT count(DISTINCT id) FROM seed_candidates').fetchone()[0]
                     if has_table(active, 'seed_candidates') else 0)
        with ledger(self.claim_path) as claims:
            observed = read_claims(claims)
        for item in observed:
            if item['source_authorization']:
                target = targets.get(identity(item))
                if not target or source_authorization(
                        target, item['source_url'], item['final_url'],
                        testing=self.testing) != item['source_authorization']:
                    item['binding_current'] = 0
        facts = assess(observed, [], policy, now=now)['facts']
        supporting = {cid for row in facts for cid in row['supporting_claim_ids']}
        unverified = {item['claim_id'] for item in observed if item['status'] == 'source_claim'} - supporting
        tavily = self._tavily(day)
        taken_at = kst_now(self.clock)
        if taken_at.date() == day and taken_at < cutoff(day):
            replace(self._path(day), json_bytes({
                'observed_at': taken_at.isoformat(), 'seeds_total': seeds, 'targets': len(targets),
                'facts_total': len(facts), 'unverified_total': len(unverified), 'tavily_used': tavily,
            }))

    def _observation(self, day, first):
        for offset in range((day - first).days + 1):
            observed_day = day - timedelta(days=offset)
            try:
                row = read_json(self._path(observed_day))
                taken_at = kst_timestamp(row['observed_at'])
                if (taken_at.date() != observed_day or taken_at >= cutoff(observed_day)
                        or not counts_valid(row, AGGREGATES | {'tavily_used'})):
                    raise ValueError('observation_invalid')
            except FileNotFoundError:
                continue
            except SOURCE_ERRORS:
                if offset:
                    continue
                raise
            if offset and self._tavily(day):
                raise TavilyCutoffUnknown
            return {**{k: row[k] for k in AGGREGATES}, 'tavily_used': 0 if offset else row['tavily_used']}
        raise ValueError('observation_missing')

    def _report(self, day, first):
        observed = self._observation(day, first)
        start, stop = datetime.combine(day, time(), KST), cutoff(day)
        bounds = (start.isoformat(), stop.isoformat())
        seeds_new = pages = claims_new = 0
        with ledger(self.active_path) as active:
            if has_table(active, 'seed_candidates'):
                seeds_new = active.execute('''SELECT count(DISTINCT id) FROM seed_candidates
                     WHERE first_seen>=? AND first_seen<?''', bounds).fetchone()[0]
            pages = fetched_pages(active, bounds)
        # Each Fetcher has one audit destination. Policy timing in the fallback
        # ledger has its own tables and contributes no duplicate request rows.
        with ledger(self.settings.active_dir / AUDIT_FILE) as fallback:
            pages += fetched_pages(fallback, bounds)
        with ledger(self.claim_path) as claims:
            if has_table(claims, 'claims'):
                claims_new = claims.execute('''SELECT count(DISTINCT c.claim_id)
                    FROM claims c JOIN receipts r USING(receipt_id)
                    WHERE r.created_at>=? AND r.created_at<?''', bounds).fetchone()[0]
        provider_calls = dict.fromkeys(PROVIDERS, 0)
        with ledger(self.model_path) as model:
            if has_table(model, 'model_usage'):
                provider_calls.update(model.execute('''SELECT provider,count(*) FROM model_usage
                    WHERE dispatched_at>=? AND dispatched_at<? AND provider IN (?,?) GROUP BY provider''',
                    (*bounds, *PROVIDERS)))
        outcomes = self.outbox.terminal_attempts(start, stop)
        return {**observed, 'date': day.isoformat(), 'seeds_new': seeds_new, 'pages_fetched': pages,
                'claims_new': claims_new, 'deploys': outcomes.count('deployed'),
                'deploy_failures': outcomes.count('failed'), 'provider_calls': provider_calls}

    @staticmethod
    def _valid_line(row):
        return (isinstance(row, dict) and row.keys() == KEYS
                and date.fromisoformat(row['date']).isoformat() == row['date']
                and isinstance(row['provider_calls'], dict)
                and set(row['provider_calls']) == set(PROVIDERS)
                and counts_valid(row['provider_calls'], PROVIDERS)
                and counts_valid(row, COUNTS))

    def _recover_append(self):
        try:
            pending = read_json(self.journal)
        except FileNotFoundError:
            return
        offset, data = pending['offset'], pending['line'].encode()
        if (type(offset) is not int or offset < 0 or not data.endswith(b'\n')
                or not self._valid_line(json.loads(data)) or json_bytes(json.loads(data)) != data):
            raise ValueError('metrics_journal_invalid')
        fd = os.open(self.output, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'r+b') as stream:
            size = stream.seek(0, os.SEEK_END)
            if size < offset or size > offset + len(data):
                raise ValueError('metrics_append_conflict')
            stream.seek(0)
            before = stream.read(offset)
            if ((before and not before.endswith(b'\n'))
                    or hashlib.sha256(before).hexdigest() != pending['prefix_sha256']):
                raise ValueError('metrics_append_conflict')
            prefix = stream.read()
            if not data.startswith(prefix):
                raise ValueError('metrics_append_conflict')
            if len(prefix) < len(data):
                stream.write(data[len(prefix):])
                stream.flush()
            os.fsync(stream.fileno())
        sync_directory(self.directory)
        self.journal.unlink()
        sync_directory(self.directory)

