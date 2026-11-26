"""Scheduled collector supervision and KST wake-up catch-ups."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from dataclasses import replace as with_settings
from datetime import timedelta
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from ..config import REPO_ROOT, Settings
from ..crawl_policy import COLLECTION
from ..db import connect
from ..discover import seeds
from ..fetcher import Fetcher
from ..locks import RunLock
from ..network_store import open_leases
from ..runner import run
from ..util.timeutil import Clock, kst_now
from ..web_proxy import resolve_run
from hanset_twin.daemon import run_once
from hanset_twin.store import connect as twin_connect, open_active_readonly
from .config import CollectSettings, DEFAULT_MODEL, configured_model, outside
from .daily_metrics import DailyMetrics, SOURCE_ERRORS
from .handoff import Outbox, kst_timestamp
from .plan import assemble_targets
from .private_io import Busy, json_bytes, lock, read, read_json, replace, sync_directory
from .schedules import DATA, GRACE, JOBS, SHARED_JOBS
from .service import public_result, run_web

DEFERRED = 75
SEED_COMPLETE = {
    'ok', 'skipped:no_key', 'skipped:no_known_sites', 'skipped:no_sitemap',
    'skipped:no_public_listing', 'tavily_key_missing',
}


def stamp(path):
    try:
        return kst_timestamp(read(path).decode().strip())
    except FileNotFoundError:
        return None


class Scheduled:
    def __init__(self, root, settings, clock=None, *, testing=False):
        self.root, self.settings = root, settings
        self.clock = clock or Clock()
        self.testing = testing
        self.stamps = root / 'stamps'
        self.checkpoint = self.stamps / 'wide_collect.completion.json'
        self.daily_checkpoint = self.stamps / 'daily_collect.completion.json'
        self.outbox = Outbox(root)
        self.metrics = DailyMetrics(root, settings, self.clock, testing=testing)

    def watchdog(self, launch):
        self.metrics.run()
        if self.outbox.ready():
            try:
                with lock(self.stamps / 'wide_collect.lock'):
                    self.finish_wide()
            except Busy:
                pass
            except SOURCE_ERRORS:
                self.diagnostic('wide_collect', 'publication_checkpoint_unavailable')
        else:
            self.diagnostic('wide_collect', 'publication_readiness_required')
        try:
            with lock(self.stamps / 'daily_collect.lock'):
                self.finish_daily()
        except Busy:
            pass
        except SOURCE_ERRORS:
            self.diagnostic('daily_collect', 'publication_checkpoint_unavailable')
        with ThreadPoolExecutor(max_workers=3) as workers:
            passes = [workers.submit(self.catch_up, group, launch)
                      for group in (SHARED_JOBS, ('seed_discovery',), ('twin',))]
            for worker in passes:
                worker.result()
        self.metrics.run()

    def catch_up(self, group, launch):
        while (due := self.eligible(group)):
            slot, name = due[0]
            try:
                result = launch(name, slot).wait()
            except OSError:
                self.diagnostic(name, 'scheduled_launch_failed')
                return
            if result == DEFERRED or (slot, name) in self.eligible(group):
                # A deferred oldest job remains first on the next pass.
                return

    def eligible(self, group, now=None):
        now = now or kst_now(self.clock)
        due = []
        for name in group:
            if name == 'wide_collect' and not self.outbox.ready():
                continue
            slot = JOBS[name].latest(now)
            if now - slot <= GRACE:
                continue
            try:
                if not self.accounted_for(name, slot):
                    due.append((slot, name))
            except SOURCE_ERRORS:
                self.diagnostic(name, 'scheduled_record_unreadable')
        # Sorting by slot is stable and retains the declared order for ties.
        return sorted(due, key=lambda entry: entry[0])

    def accounted_for(self, name, slot):
        success = stamp(self.stamps / f'{name}.ok')
        attempted = stamp(self.stamps / f'{name}.attempt')
        if attempted is not None and JOBS[name].latest(attempted) != attempted:
            raise ValueError('scheduled_attempt_invalid')
        return (success is not None and success >= slot) or (attempted is not None and attempted >= slot)

    def dispatch_history(self):
        """Check for declared job stamps by presence alone."""
        return any((self.stamps / f'{name}.{kind}').exists()
                   for name in JOBS for kind in ('ok', 'attempt'))

    def defers_to_older(self, name, slot, catchup, due):
        if not due:
            return False
        if catchup is not None:
            return due[0] != (slot, name)
        if name != 'active':
            return due[0] != (slot, name) and due[0][0] <= slot
        if not self.dispatch_history():
            return False
        # Each peer slot can delay ordinary active for one hourly slot.
        previous = JOBS[name].latest(slot - timedelta(seconds=1))
        return any(other != name and previous < peer_slot <= slot
                   for peer_slot, other in due)

    def run_job(self, name, *, catchup=None):
        try:
            return self._run_job(name, catchup)
        except Busy:
            return (DEFERRED if catchup else 0), 'already_running'
        except SOURCE_ERRORS:
            return 1, f'{name} scheduled_record_or_payload_unavailable'
        finally:
            self.metrics.run()

    def _run_job(self, name, catchup):
        group = SHARED_JOBS if name in SHARED_JOBS else (name,)
        admission = 'collector' if name in SHARED_JOBS else name
        with ExitStack() as payload_locks:
            with lock(self.stamps / f'admission-{admission}.lock'):
                job_fd = payload_locks.enter_context(lock(self.stamps / f'{name}.lock'))
                if name == 'wide_collect':
                    if not self.outbox.ready() or os.environ.get('HANSET_AUTO_PUBLISH') != '1':
                        return 0, 'publication_readiness_required'
                    self.finish_wide()
                elif name == 'daily_collect':
                    self.finish_daily()
                now = kst_now(self.clock)
                slot = JOBS[name].latest(now)
                if self.accounted_for(name, slot):
                    return 0, 'slot_complete'
                if catchup is not None and (slot != catchup or now - slot <= GRACE):
                    return 0, 'slot_deferred'
                due = self.eligible(group, now)
                if self.defers_to_older(name, slot, catchup, due):
                    return (DEFERRED if catchup else 0), 'older_slot_eligible'
                fds = [job_fd]
                for path in self.payload_paths(name):
                    fds.append(payload_locks.enter_context(lock(path)))

                # Slots can roll over while acquiring the final lock.
                now = kst_now(self.clock)
                if JOBS[name].latest(now) != slot or self.accounted_for(name, slot):
                    return 0, 'slot_deferred'
                due = self.eligible(group, now)
                if self.defers_to_older(name, slot, catchup, due):
                    return (DEFERRED if catchup else 0), 'older_slot_eligible'
                # Record the slot before launch so it survives supervisor death.
                self.write_stamp(name, 'attempt', slot)
                child = self.start_payload(name, slot, fds)
                if name == 'active':
                    print('run_start', flush=True)

            result = child.wait()
            if name == 'active':
                print(f'run_exit={result}', flush=True)
            if result == 0:
                if name == 'wide_collect':
                    if not self.finish_wide(completed=kst_now(self.clock), expected_slot=slot):
                        return 1, 'publication_checkpoint_missing'
                elif self.publishes_daily(name):
                    if not self.finish_daily(completed=kst_now(self.clock), expected_slot=slot):
                        return 1, 'publication_checkpoint_missing'
                else:
                    self.write_stamp(name, 'ok', kst_now(self.clock))
            elif name == 'wide_collect':
                self.discard_checkpoint(self.checkpoint)
            elif name == 'daily_collect':
                self.discard_checkpoint(self.daily_checkpoint)
            return (result if result >= 0 else 128 - result), 'complete' if result == 0 else 'payload_incomplete'

    def payload_paths(self, name):
        settings = self.settings
        if name == 'twin':
            return (settings.twin_dir / 'daemon.lock',)
        if name == 'seed_discovery':
            return (settings.active_dir / 'seed-discovery-cron.lock', settings.active_dir / 'seeds-discover.lock')
        outer = ((settings.active_dir / 'cron.lock',) if name == 'active' else
                 (settings.data_dir / 'cron.lock', settings.data_dir / 'daemon.lock'))
        return (*outer, settings.active_dir / 'run.lock')

    def start_payload(self, name, slot, fds):
        command = ['python3', '-m', 'hanset_active.collect.scheduled',
                   name, '--payload', slot.isoformat(), *(str(fd) for fd in fds)]
        if JOBS[name].timeout:
            command = ['timeout', *JOBS[name].timeout, *command]
        return subprocess.Popen(command, pass_fds=tuple(fds))

    def payload(self, name, slot, fds):
        paths = (self.stamps / f'{name}.lock', *self.payload_paths(name))
        if len(paths) != len(fds):
            raise ValueError('inherited_lock_invalid')
        for path, fd in zip(paths, fds):
            if not os.path.samestat(path.stat(), os.fstat(fd)):
                raise ValueError('inherited_lock_invalid')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        settings, clock = self.settings, self.clock
        if name == 'twin':
            with ExitStack() as stack:
                twin = stack.enter_context(closing(twin_connect(settings.twin_dir / 'twin.sqlite3')))
                path = settings.active_dir / 'active.sqlite3'
                active = stack.enter_context(closing(open_active_readonly(path))) if path.exists() else None
                run_id, state, stats = run_once(twin, active, csv_path=REPO_ROOT / 'saunas.csv',
                                               lock_path=paths[-1], clock=clock, admission=True, structured_only=True)
                print(json.dumps({'run_id': run_id, 'state': state, 'stats': stats}))
                return state == 'ok'

        with ExitStack() as stack:
            if name in ('active', 'seed_discovery'):
                snapshot = assemble_targets(settings, now=kst_now(clock), testing=self.testing,
                                            include_watchlist=False, bounded=True)
                if snapshot['db_status'] != 'ok':
                    self.diagnostic(name, 'active_authorization_unavailable')
                    return False
                # Only the explicit `hanset_active init` command bootstraps the store.
                lease_db = stack.enter_context(closing(connect(settings.active_dir / 'active.sqlite3', create=False)))
            else:
                lease_db = stack.enter_context(closing(open_leases(settings.active_dir)))
            kwargs = {'filename': paths[-1].name, 'lease_key': 'seed_discovery'} if name == 'seed_discovery' else {}
            guard = RunLock(lease_db, settings.active_dir, uuid4().hex, clock=clock, **kwargs)
            with guard.inherited_lease():
                return self.collect(name, slot, guard, collector_lock_held=name in ('daily_collect', 'wide_collect'))

    def collect(self, name, slot, guard, *, collector_lock_held=False):
        settings, clock = self.settings, self.clock
        active = Settings(data_dir=settings.active_dir, testing=self.testing)
        if name == 'seed_discovery':
            result = seeds.discover(active, guard.db, clock=clock, admission=guard)
            print(json.dumps(result, ensure_ascii=False))
            return (result['state'] == 'ok' and result['sources'].keys() == set(seeds.SOURCES)
                    and all(row['status'] in SEED_COMPLETE for row in result['sources'].values()))
        if name == 'active':
            egress, blocked, _ = resolve_run(clock=clock, host_dir=settings.active_dir)
            if blocked:
                print(json.dumps({'state': 'stopped', 'reason': blocked}))
                return False
            fetcher = Fetcher(active, guard.db, clock, egress=egress, public_scope=True, policy=COLLECTION)
            fetcher.facts_only = True
            try:
                result = run(fetcher, max_minutes=25, admission=guard)
            finally:
                fetcher.close()
            if fallback := egress.public():
                result['web_egress'] = fallback
            print(json.dumps(result))
            return result.get('checkpoint_complete') is True

        result = run_web(with_settings(settings, model=configured_model()), clock=clock,
                         admission=guard, testing=self.testing, collector_lock_held=collector_lock_held)
        print(json.dumps(public_result(result), ensure_ascii=False))
        complete = result.get('success') is True or result.get('state') == 'no_targets'
        if complete and (name == 'wide_collect' or self.publishes_daily(name)):
            changes = result['changes']
            export = {'reason': 'no_events'} if changes['reason'] == 'no_events' else changes['export']
            if changes['reason'] not in ('ok', 'no_events') or changes.get('recovery_reason'):
                return False
            request = self.outbox.prepare(settings.data_dir, export, slot, job=name)
            path = self.checkpoint if name == 'wide_collect' else self.daily_checkpoint
            replace(path, json_bytes({'slot': slot.isoformat(), 'request': request,
                                                 'completed_at': kst_now(clock).isoformat()}))
        return complete

    @staticmethod
    def publishes_daily(name):
        return name == 'daily_collect' and os.environ.get('HANSET_AUTO_PUBLISH') == '1'

    def finish_wide(self, *, completed=None, expected_slot=None):
        return self.finish('wide_collect', self.checkpoint, completed, expected_slot)

    def finish_daily(self, *, completed=None, expected_slot=None):
        return self.finish('daily_collect', self.daily_checkpoint, completed, expected_slot)

    def finish(self, name, path, completed, expected_slot):
        try:
            saved = read_json(path)
        except FileNotFoundError:
            return False
        slot = kst_timestamp(saved['slot'])
        saved_at = kst_timestamp(saved['completed_at'])
        request = saved['request']
        if (JOBS[name].latest(slot) != slot or saved_at < slot
                or (expected_slot is not None and slot != expected_slot)
                or (request is not None and (request['slot'] != saved['slot']
                    or request.get('job', 'wide_collect') != name))):
            raise ValueError('publication_checkpoint_invalid')
        if request is not None:
            self.outbox.expose(request)
        success = stamp(self.stamps / f'{name}.ok')
        completed = completed or saved_at
        if success is None or success < completed:
            self.write_stamp(name, 'ok', completed)
        # Recovery can expose a request without attributing it to a new run.
        token = os.environ.get('HANSET_RUN_TOKEN') if expected_slot is not None else None
        replace(self.stamps / f'{name}.publication.json', json_bytes({
            'schema_version': 1, 'job': name, 'slot': saved['slot'],
            'request_id': request['request_id'] if request is not None else None,
            'exposed_at': kst_now(self.clock).isoformat(), 'run_token': token,
        }))
        self.discard_checkpoint(path)
        return True

    def discard_checkpoint(self, path):
        try:
            path.unlink()
        except FileNotFoundError:
            return
        sync_directory(self.stamps)

    def write_stamp(self, name, extension, slot):
        replace(self.stamps / f'{name}.{extension}', (slot.isoformat() + '\n').encode())

    @staticmethod
    def diagnostic(name, status):
        print(f'{name} {status}', file=sys.stderr)


def launch(name, slot):
    job = JOBS[name]
    env = {**os.environ, 'HANSET_CATCHUP_SLOT': slot.isoformat()}
    if name == 'wide_collect':
        env['HANSET_AUTO_PUBLISH'] = '1'
    return subprocess.Popen([str(REPO_ROOT / 'scripts' / job.script), *job.args], env=env)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job', choices=(*JOBS, 'watchdog'))
    parser.add_argument('options', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    os.environ.pop('HANSET_ACTIVE_DEBUG', None)
    try:
        root = outside(Path(os.environ.get('HANSET_DATA_DIR', DATA)))
        paths = {key: Path(os.environ.get(env, root / child)) for key, env, child in (
            ('data_dir', 'HANSET_IG_DIR', 'ig'), ('active_dir', 'HANSET_ACTIVE_DIR', 'active'),
            ('twin_dir', 'HANSET_TWIN_DIR', 'twin'))}
        settings = CollectSettings(**paths, model=DEFAULT_MODEL)
        for key, value in zip(('HANSET_IG_DIR', 'HANSET_ACTIVE_DIR', 'HANSET_TWIN_DIR'),
                              (settings.data_dir, settings.active_dir, settings.twin_dir)):
            os.environ[key] = str(value)
        scheduled = Scheduled(root, settings)
        if args.job in JOBS and args.options[:1] == ['--payload']:
            slot, *fds = args.options[1:]
            return 0 if scheduled.payload(args.job, kst_timestamp(slot), list(map(int, fds))) else 1
        if args.options:
            parser.error('unexpected_options')
        if args.job == 'watchdog':
            scheduled.watchdog(launch)
            return 0
        raw = os.environ.get('HANSET_CATCHUP_SLOT')
        result, status = scheduled.run_job(args.job, catchup=kst_timestamp(raw) if raw else None)
        print(status)
        return result
    except (Exception, KeyboardInterrupt):
        Scheduled.diagnostic(args.job, 'scheduled_error')
        return 1


if __name__ == '__main__':
    sys.exit(main())
