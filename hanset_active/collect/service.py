"""Web runs and desktop Instagram ingestion. No browser session is read or launched."""
import json
import re
import sqlite3
from collections import OrderedDict
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

from hanset_active.config import Settings
from hanset_active.db import connect as active_connect
from hanset_active import web_proxy
from hanset_active.fetcher import Fetcher
from hanset_active.locks import AlreadyRunning as ActiveBusy
from hanset_active.locks import RunLock
from hanset_active.util.pii import strip_pii
from hanset_active.util.timeutil import Clock, kst_now
from hanset_active.util.urls import registrable_domain
from hanset_twin.daemon import AlreadyRunning as CollectorBusy
from hanset_twin.daemon import ProcessLock

from . import CollectError
from .config import IG_READ_MAX, MAX_TARGETS, PAGE_DAY_CAP, PAGE_RUN_CAP, SUBPAGE_MAX, CollectSettings, domain_allowed
from .deepseek import OUTPUT_TOKENS, DeepSeek, credentials_present
from .ground import INSTRUCTION, SUMMARY_INSTRUCTION, parse_items, payload_items, summary_items
from .ingest import load_watchlist, parse_batch, security_flag, within_window
from .state import State
from .store import connect as claim_connect
from .store import (digest, record, prune_web_target_blocks, recent_web_target_block,
                    remember_web_target_block, remember_web_target_observation, web_target_history)
from .targets import canonical_requested_url, fetch_url_allowed, resolve_jobs
from .web import classify_page, select_subpages
from ..crawl_policy import COLLECTION
from .plan import assemble_targets, ensure_daily_plan, load_targets, local_target_order, order_targets
from .watchlist_proposals import inspect_page, write_proposals

RETRYABLE = frozenset({
    'model_timeout', 'model_transport', 'model_rate_limited', 'model_invalid',
    'model_unavailable', 'model_auth', 'model_budget_exhausted',
    'model_insufficient_balance',
})
def _base(state_name, reason, *, success, collected, mode, pages=None, rejections=None):
    body = {
        'state': state_name,
        'reason': reason,
        'success': success,
        'collected': collected,
        'mode': mode,
        'pages': pages or [],
    }
    if mode == 'web':
        body['proxy_heal'] = web_proxy.new_heal_status()
    if rejections:
        body['rejections'] = rejections
    return body


def public_result(result):
    pages = []
    for page in result.get('pages') or []:
        if not isinstance(page, dict):
            continue
        url = page.get('url') if isinstance(page.get('url'), str) else ''
        disposition = page.get('disposition') if isinstance(page.get('disposition'), str) else 'error'
        if len(disposition) > 80 or not disposition.replace('_', '').isalnum():
            disposition = 'error'
        pages.append({'url': strip_pii(url)[:500], 'disposition': disposition})
    out = {
        'state': result.get('state') if isinstance(result.get('state'), str) else 'error',
        'reason': result.get('reason') if isinstance(result.get('reason'), str) else 'runtime_error',
        'success': bool(result.get('success')),
        'collected': bool(result.get('collected')),
        'mode': result.get('mode') if result.get('mode') in ('web', 'instagram', 'status') else 'web',
        'pages': pages[:PAGE_DAY_CAP],
        'counts': result.get('counts') if isinstance(result.get('counts'), dict) else {},
        'budgets': result.get('budgets') if isinstance(result.get('budgets'), dict) else {},
    }
    if isinstance(result.get('at'), str):
        out['at'] = result['at']
    rejections = result.get('rejections')
    if isinstance(rejections, dict) and all(
            isinstance(key, str) and key.replace('_', '').isalnum() and type(value) is int and 0 <= value <= 1000
            for key, value in rejections.items()):
        out['rejections'] = dict(rejections)
    providers = result.get('provider')
    if isinstance(providers, dict) and all(
            key in ('deepseek', 'qwen') and type(value) is int and 0 <= value <= 1000
            for key, value in providers.items()):
        out['provider'] = dict(providers)
    if result.get('primary_skipped') == 'insufficient_balance':
        out['primary_skipped'] = 'insufficient_balance'
    heal = result.get('proxy_heal')
    if (isinstance(heal, dict) and
            set(heal) == {'attempted', 'heal_exit', 'retries', 'recovered'} and
            type(heal['attempted']) is bool and type(heal['recovered']) is bool and
            (heal['heal_exit'] is None or type(heal['heal_exit']) is int) and
            type(heal['retries']) is int and 0 <= heal['retries'] <= 3):
        out['proxy_heal'] = {key: heal[key] for key in (
            'attempted', 'heal_exit', 'retries', 'recovered')}
    egress = result.get('web_egress')
    if (out['mode'] in ('web', 'status') and isinstance(egress, dict)
            and set(egress) == {'mode', 'reason', 'phase'} and egress['mode'] == 'direct'
            and egress['reason'] == 'proxy_unavailable' and egress['phase'] in ('preflight', 'mid_run')):
        out['web_egress'] = dict(egress)
    blob = json.dumps(out, ensure_ascii=False)
    from .ground import secrets
    for secret in secrets():
        blob = blob.replace(secret, '[redacted]')
    return json.loads(blob)


def _finish(state, body, clock):
    body['at'] = kst_now(clock).isoformat()
    body['counts'] = dict(state.data['counts'])
    body['budgets'] = dict(state.data['budgets'])
    body['provider'] = dict(state.model_providers)
    if state.primary_skipped == 'insufficient_balance':
        body['primary_skipped'] = 'insufficient_balance'
    if state.proxy_heal is not None:
        body['proxy_heal'] = dict(state.proxy_heal)
    clean = public_result(body)
    state.write_status(clean)
    return clean


def _prepare_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def _chunks(rows, *, kind):
    encode = summary_items if kind == 'instagram' else payload_items
    batches, current = [], []
    for row in rows:
        trial = current + [row]
        encoded = encode([{'text': item['text']} for item in trial])
        if current and len(json.dumps(encoded, ensure_ascii=False)) > 20_000:
            batches.append(current)
            current = [row]
        else:
            current = trial
    if current:
        batches.append(current)
    return batches


def _store_batch(db, rows, parsed, *, kind, clock, state):
    pages = []
    new_claims = 0
    for row, (claims, _dropped) in zip(rows, parsed):
        text_hash = digest(row['text'])
        existing = db.execute(
            'SELECT disposition FROM receipts WHERE source_kind=? AND source_url=? AND content_hash=?',
            (kind, row['url'], text_hash)).fetchone()
        if existing:
            disposition, ids = 'duplicate', []
        else:
            disposition, ids = record(
                db, kind=kind, url=row['url'], text_hash=text_hash, disposition='applied',
                claims=claims, venue_id=row['venue_id'], venue_key=row['venue_key'],
                seen_at=row['seen_at'], clock=clock, source_type=row.get('source_type'))
        if kind == 'web':
            _observe_stored(db, row, text_hash, disposition)
            state.complete(row['url'], disposition)
        else:
            state.data['counts']['ig_reads'] += 1
            state.complete(row['url'], disposition)
        state.data['counts']['claims'] += len(ids)
        new_claims += len(ids)
        state.save()
        pages.append({'url': row['url'], 'disposition': disposition})
    return pages, new_claims


def _observe_stored(db, row, text_hash, disposition):
    from .changes import insert_observation
    from .store import read_claims
    remember_web_target_observation(
        db, row['url'], disposition, content_hash=text_hash, observed_at=row['seen_at'],
        testing=row.get('_testing', False))
    from hanset_active.util.urls import is_excluded
    # Keep evidence for declined venues out of the change feeds used by
    # publication and outreach workflows.
    if is_excluded(row.get('name', ''), row['url'], row.get('final_url', '')):
        return
    claims = [claim for claim in read_claims(db) if claim['content_hash'] == text_hash
              and claim['source_url'] == row['url'] and claim['venue_id'] == row['venue_id']
              and claim['venue_key'] == row['venue_key']]
    insert_observation(db, {**row, 'content_hash': text_hash}, claims)


def _extract(rows, *, kind, settings, state, session, clock):
    db = claim_connect(settings.data_dir)
    pages, claims = [], 0
    brain = DeepSeek(state, model=settings.model, session=session)
    try:
        for batch in _chunks(rows, kind=kind):
            fresh = []
            for row in batch:
                text_hash = digest(row['text'])
                if db.execute(
                        'SELECT 1 FROM receipts WHERE source_kind=? AND source_url=? AND content_hash=?',
                        (kind, row['url'], text_hash)).fetchone():
                    if kind == 'instagram':
                        state.data['counts']['ig_reads'] += 1
                    else:
                        _observe_stored(db, row, text_hash, 'duplicate')
                    state.complete(row['url'], 'duplicate')
                    pages.append({'url': row['url'], 'disposition': 'duplicate'})
                else:
                    fresh.append(row)
            if not fresh:
                continue
            if not state.can_model(1, OUTPUT_TOKENS):
                raise CollectError('model_budget_exhausted')
            documents = [{'text': row['text']} for row in fresh]
            if kind == 'instagram':
                answer = brain.ask(summary_items(documents), instruction=SUMMARY_INSTRUCTION)
            else:
                answer = brain.ask(payload_items(documents), instruction=INSTRUCTION)
            parsed = parse_items(answer, documents)
            stored, added = _store_batch(db, fresh, parsed, kind=kind, clock=clock, state=state)
            pages.extend(stored)
            claims += added
    finally:
        brain.close()
        db.close()
    return pages, claims


def _default_search(host_dir):
    from hanset_active.search import discover_known_venue
    from hanset_active.discover.seeds import TavilyRun

    run = TavilyRun()

    def search(*args, **kwargs):
        outcome = discover_known_venue(*args, dispatch_guard=run.search_allowed, host_dir=host_dir, **kwargs)
        if outcome.reason in ('tavily_provider_quota', 'tavily_access_blocked', 'tavily_rate_limited'):
            run.stop_on(outcome.reason)
        return outcome

    return search


def run_web(settings, *, targets_path=None, clock=None, session=None, search=None, testing=False,
            planner=None, planner_session=None):
    clock = clock or Clock()
    mode = 'web'
    try:
        _prepare_dir(settings.data_dir)
        with ProcessLock(settings.data_dir / 'daemon.lock'):
            recovery = _change_cycle(settings, targets_path=targets_path, testing=testing, export=False, clock=clock)
            try:
                result = _run_web(settings, targets_path=targets_path, clock=clock, session=session, search=search,
                                  testing=testing, planner=planner, planner_session=planner_session)
            finally:
                changes = _change_cycle(settings, targets_path=targets_path, testing=testing, clock=clock)
            result['changes'] = changes
            if recovery.get('reason') not in ('ok', 'no_events'):
                result['changes']['recovery_reason'] = recovery['reason']
            # Persist only fixed side diagnostics, never exception strings.
            status = settings.data_dir / 'status.json'
            if status.exists():
                from .state import atomic_json
                try:
                    atomic_json(status, result)
                except (CollectError, OSError):
                    pass  # Side-artifact status must not invalidate collection.
            return result
    except CollectorBusy:
        return public_result(_base('already_running', 'already_running', success=False, collected=False, mode=mode))
    except CollectError as error:
        return public_result(_base('error', error.reason, success=False, collected=False, mode=mode))


def _rotate_seed_jobs(pending, last_fetched):
    """Use seed history within the normal queue while retaining plan ties."""
    if not any(job['seed_url'] for job in pending.values()):
        return pending
    fresh, fresh_seeds, revisits = [], [], []
    for url, job in pending.items():
        item = (url, job)
        if digest(url) in last_fetched:
            revisits.append(item)
        else:
            (fresh_seeds if job['seed_url'] else fresh).append(item)
    # Keep nonseed revisit positions and fill seed positions oldest first.
    seed_revisits = iter(sorted((item for item in revisits if item[1]['seed_url']),
                                key=lambda item: last_fetched[digest(item[0])]))
    return OrderedDict(fresh + fresh_seeds + [next(seed_revisits) if job['seed_url'] else (url, job)
                                             for url, job in revisits])


def _run_web(settings, *, targets_path, clock, session, search, testing, planner=None, planner_session=None):
    state = State(settings.data_dir, clock)
    state.proxy_heal = web_proxy.new_heal_status()
    path = Path(targets_path) if targets_path else settings.data_dir / 'web_targets.json'
    if targets_path:
        from .config import outside
        path = outside(path)
    if targets_path:
        targets, targets_status = load_targets(path, testing=testing)
        # Ad-hoc input never reads/writes the daily plan or invokes its model.
        targets = local_target_order(settings, targets, now=kst_now(clock))
        planning_notes = [{'code': 'adhoc_targets_local_order', 'count': 1}]
    else:
        assembled = assemble_targets(settings, now=kst_now(clock), testing=testing)
        targets, targets_status = assembled['targets'], assembled['targets_status']
        plan = ensure_daily_plan(settings, state, targets, targets_status, clock=clock,
                                 planner=planner, planner_session=planner_session, testing=testing,
                                 assembled=assembled)
        targets, planning_notes = order_targets(
            local_target_order(settings, assembled['authorization'], now=kst_now(clock), materialized=assembled['snapshot']), plan)
    if targets_status == 'invalid':
        return _finish(state, _base('error', 'targets_invalid', success=False, collected=False, mode='web'), clock)
    if not targets:
        return _finish(state, _base('no_targets', 'no_targets', success=False, collected=False, mode='web'), clock)
    active_settings = Settings(
        data_dir=settings.active_dir,
        testing=testing,
        allow_private_hosts=testing,
        min_interval=0 if testing else 5,
        global_gap=0 if testing else 1,
    )
    active = active_connect(active_settings.data_dir / 'active.sqlite3')
    history = None
    skipped = {}
    deferred_urls = set()
    unsafe = 0
    egress = None

    def finish_web(body):
        # Fetch/extraction results take precedence over cooldown notes in the
        # bounded public page list; the aggregate retains the full skip count.
        body['pages'].extend({'url': url, 'disposition': 'recent_block_skipped'} for url in skipped)
        rejections = dict(body.get('rejections') or {})
        if skipped:
            rejections['recent_block_skipped'] = len(skipped)
        if unsafe:
            rejections['unsafe_url_skipped'] = unsafe
        if deferred_urls:
            rejections['recent_unchanged_deferred'] = len(deferred_urls)
        if rejections:
            body['rejections'] = rejections
        if egress:
            body['web_egress'] = egress
        return _finish(state, body, clock)

    def cooling(url):
        if recent_web_target_block(history, url, clock=clock, testing=testing):
            skipped[url] = True
            return True
        return False

    try:
        history = claim_connect(settings.data_dir)
        prune_web_target_blocks(history, clock=clock)
        needs_search = any(item['tavily'] and not item['official_urls'] for item in targets)
        jobs, notes = resolve_jobs(
            targets,
            search=search if search is not None or not needs_search else _default_search(active_settings.data_dir),
            db=active,
            clock=clock,
            testing=testing,
        )
        last_fetched, unchanged = web_target_history(history, clock=clock, testing=testing)
        notes.extend({'url': '', 'disposition': note['code']} for note in planning_notes)
        completed = set()
        for url in state.data['completed']:
            # The checkpoint is shared with same-day Instagram ingestion. Its
            # denied instagram.com keys (and any other unsafe key) stay inert,
            # exactly as with raw string membership; they never abort the run.
            try:
                completed.add(canonical_requested_url(url, testing=testing))
            except (ValueError, TypeError):
                continue
        scheduled = {}
        for job in jobs:
            try:
                url = canonical_requested_url(job['url'], testing=testing)
            except (ValueError, TypeError):
                # Search leads are only scheme/domain-checked upstream. An
                # unsafe identity (credential params, redaction markers,
                # over-length) skips this job, never the whole run. The raw
                # URL is not echoed into status notes.
                unsafe += 1
                notes.append({'url': '', 'disposition': 'unsafe_url_skipped'})
                continue
            if cooling(url) or url in completed:
                continue
            if url in scheduled:
                if job['seed_url'] or not scheduled[url]['seed_url']:
                    continue
                # A manual or gap URL owns this fetch even if the seed ranked first.
                del scheduled[url]
            scheduled[url] = {**job, 'depth': 0}
        pending, deferred = OrderedDict(), OrderedDict()
        for url, job in scheduled.items():
            # Canonical keys deduplicate work; explicit receipt URLs retain
            # their existing identity (including a trailing slash).
            queue = pending
            if digest(url) in unchanged:
                deferred_urls.add(url)
                queue = deferred
            queue[url] = job
        pending = _rotate_seed_jobs(pending, last_fetched)
        deferred = _rotate_seed_jobs(deferred, last_fetched)
        # Apply the target window after cooldown filtering and fair rotation.
        selected = set()
        for queue in (pending, deferred):
            for url, job in list(queue.items()):
                key = (job['venue_id'], job['venue_key'])
                if key not in selected and len(selected) >= MAX_TARGETS:
                    del queue[url]
                else:
                    selected.add(key)
        if not pending and not deferred and skipped:
            return finish_web(_base('done', 'recent_block_skipped', success=True, collected=False, mode='web', pages=notes))
        if not pending and not deferred and jobs and unsafe == len(jobs):
            return finish_web(_base('stopped', 'unsafe_url_skipped', success=False, collected=False, mode='web', pages=notes))
        if not pending and not deferred and jobs:
            return finish_web(_base('already_complete', 'already_complete', success=True, collected=False, mode='web', pages=notes))
        if not pending and not deferred:
            reason = notes[0]['disposition'] if notes else 'no_urls'
            return finish_web(_base('stopped', reason, success=False, collected=False, mode='web', pages=notes))
        if state.data['counts']['pages'] >= PAGE_DAY_CAP:
            return finish_web(_base('stopped', 'daily_cap', success=False, collected=False, mode='web', pages=notes))
        if not credentials_present():
            return finish_web(_base('error', 'missing_credentials', success=False, collected=False, mode='web', pages=notes))
        if not state.can_model(1, OUTPUT_TOKENS):
            return finish_web(_base('stopped', 'model_budget_exhausted', success=False, collected=False, mode='web', pages=notes))
        try:
            proxy = web_proxy.configured()
        except ValueError:
            return finish_web(_base('stopped', 'proxy_invalid', success=False, collected=False, mode='web', pages=notes))
        if proxy:
            blocked, state.proxy_heal = web_proxy.preflight_with_retry(proxy, clock=clock)
            if blocked == 'proxy_unavailable':
                proxy = ''
                egress = {'mode': 'direct', 'reason': blocked, 'phase': 'preflight'}
            elif blocked:
                return finish_web(_base('stopped', blocked, success=False, collected=False, mode='web', pages=notes))
        ready, pages = [], list(notes)
        proposal_evidence = []
        stop_reason = None
        try:
            with RunLock(active, active_settings.data_dir, 'collect-web', clock=clock):
                fetcher = Fetcher(active_settings, active, clock, proxy=proxy, public_scope=True, policy=COLLECTION)
                try:
                    run_pages = 0
                    attempted, children = set(), {}
                    while pending or deferred:
                        job_key, job = (pending or deferred).popitem(last=False)
                        if cooling(job_key) or job_key in completed or job_key in attempted:
                            continue
                        if state.data['counts']['pages'] >= PAGE_DAY_CAP:
                            stop_reason = 'daily_cap'
                            break
                        if run_pages >= PAGE_RUN_CAP:
                            stop_reason = 'run_cap'
                            break
                        attempted.add(job_key)
                        scope = job['scope_domains'] & {registrable_domain(job_key)}
                        url_allowed = partial(fetch_url_allowed, scope_domains=scope, testing=testing)
                        fetch_options = {'preserve_path': True} if job['discovery_url'] else {}
                        fetched = fetcher.fetch(job['url'], subpage=bool(job.get('discovered_subpage')),
                                                url_allowed=url_allowed, **fetch_options)
                        proxy_outage = fetched.outcome == 'error' and fetched.reason == 'proxy_unavailable'
                        if proxy_outage:
                            egress = {'mode': 'direct', 'reason': 'proxy_unavailable', 'phase': 'mid_run'}
                        if fetched.outcome == 'cap_reached':
                            stop_reason = 'active_body_cap'
                            break
                        seen_at = kst_now(clock).isoformat()
                        state.data['counts']['pages'] += 1
                        run_pages += 1
                        state.save()
                        disposition, text = classify_page(fetched, scope)
                        final = fetched.url or job['url']
                        authorization = job['source_authorization']
                        if authorization:
                            try:
                                final_key = canonical_requested_url(final, testing=testing)
                            except (ValueError, TypeError):
                                authorization = None
                            else:
                                authorization = ({**authorization, 'final_url': final_key}
                                                 if registrable_domain(final_key) in scope
                                                 and domain_allowed(final_key, job['domains']) else None)
                        remember_web_target_block(history, job['url'], disposition,
                                                  status=fetched.status, clock=clock, testing=testing)
                        # A failed extraction must leave a retryable latest outcome,
                        # rather than cooling on an older successful receipt.
                        remember_web_target_observation(
                            history, job['url'], 'pending_extraction' if disposition == 'ready' else disposition,
                            content_hash=digest(text) if text is not None else (fetched.content_hash or None),
                            observed_at=seen_at, testing=testing)
                        target_key = (job['venue_id'], job['venue_key'])
                        if (job['depth'] == 0 and authorization and fetched.outcome == 'ok'
                                and disposition in ('ready', 'empty_body')
                                and children.get(target_key, 0) < SUBPAGE_MAX):
                            fresh_children, deferred_children = [], []
                            slots = SUBPAGE_MAX - children.get(target_key, 0)
                            for fetch_url in select_subpages(fetched.content, fetched.url or job['url'],
                                                             job['domains'], testing=testing, limit=None):
                                child_key = canonical_requested_url(fetch_url, testing=testing)
                                if child_key in completed or child_key in attempted or cooling(child_key):
                                    continue
                                if digest(child_key) in unchanged:
                                    deferred_children.append((child_key, fetch_url, deferred))
                                else:
                                    fresh_children.append((child_key, fetch_url, pending))
                                if len(fresh_children) == slots:
                                    break
                            selected = (fresh_children + deferred_children)[:slots]
                            follow = []
                            for child_key, fetch_url, queue in selected:
                                # Promote an explicit job without losing its exact-URL
                                # authorization or request mode. A queued deferred job
                                # also keeps its place within the deferred queue.
                                child = queue.get(child_key)
                                if child is None:
                                    child = {**job, 'url': fetch_url, 'source_authorization': None,
                                             'discovered_subpage': True}
                                queue[child_key] = {**child, 'depth': 1}
                                if queue is deferred:
                                    deferred_urls.add(child_key)
                                else:
                                    follow.append(child_key)
                            for child_key in reversed(follow):
                                pending.move_to_end(child_key, last=False)
                            children[target_key] = children.get(target_key, 0) + len(selected)
                        if disposition != 'ready':
                            if not proxy_outage:
                                state.complete(job['url'], disposition)
                            pages.append({'url': job['url'], 'disposition': disposition})
                            continue
                        row = {**job, 'source_authorization': authorization,
                               'source_type': 'own_site' if authorization else 'third_party',
                               'text': text, 'seen_at': seen_at, 'final_url': final, '_testing': testing}
                        proposal_evidence.extend(inspect_page(
                            row, fetched, observed_at=seen_at, disposition=disposition))
                        ready.append(row)
                finally:
                    fetcher.close()
        except ActiveBusy:
            return finish_web(_base('deferred', 'active_busy', success=False, collected=False, mode='web', pages=pages))
        if proposal_evidence:
            try:
                write_proposals(settings, proposal_evidence, day=kst_now(clock).date().isoformat())
            except (CollectError, OSError):
                # Proposals are a side artifact; never abort extraction of pages
                # that were already fetched and passed every guard.
                pages.append({'url': '', 'disposition': 'proposals_invalid'})
        extracted, claims = [], 0
        if ready:
            try:
                extracted, claims = _extract(ready, kind='web', settings=settings, state=state, session=session, clock=clock)
            except CollectError as error:
                pages.extend(extracted)
                reason = error.reason if error.reason in RETRYABLE else 'runtime_error'
                return finish_web(_base('partial', reason, success=False, collected=claims > 0, mode='web', pages=pages))
        pages.extend(extracted)
        if stop_reason == 'run_cap':
            return finish_web(_base('done', 'run_cap', success=True, collected=claims > 0, mode='web', pages=pages))
        if stop_reason:
            return finish_web(_base('partial', stop_reason, success=False, collected=claims > 0, mode='web', pages=pages))
        return finish_web(_base('done', 'ok', success=True, collected=claims > 0, mode='web', pages=pages))
    except (sqlite3.Error, ValueError, TypeError):
        raise CollectError('state_corrupt') from None
    finally:
        if history is not None:
            history.close()
        active.close()


def run_ingest(settings, *, inbox_file=None, clock=None, session=None):
    clock = clock or Clock()
    mode = 'instagram'
    try:
        _prepare_dir(settings.data_dir)
        _prepare_dir(settings.inbox)
        with ProcessLock(settings.data_dir / 'daemon.lock'):
            return _run_ingest(settings, inbox_file=inbox_file, clock=clock, session=session)
    except CollectorBusy:
        return public_result(_base('already_running', 'already_running', success=False, collected=False, mode=mode))
    except CollectError as error:
        return public_result(_base('error', error.reason, success=False, collected=False, mode=mode))


def _select_inbox(settings, inbox_file):
    inbox = settings.inbox.resolve()
    if inbox_file is None:
        files = sorted(path for path in inbox.glob('*.jsonl') if path.is_file() and not path.is_symlink())
        if not files:
            return None
        if len(files) > 1:
            raise CollectError('input_ambiguous')
        return files[0]
    from .config import outside
    path = outside(inbox_file)
    if path.is_symlink() or not path.is_file() or inbox not in path.parents:
        raise CollectError('input_outside_inbox')
    return path


def _run_ingest(settings, *, inbox_file, clock, session):
    state = State(settings.data_dir, clock)
    if state.halted():
        return _finish(state, _base('halted_today', state.data['halt']['reason'], success=False, collected=False, mode='instagram'), clock)
    if not within_window(clock):
        return _finish(state, _base('outside_window', 'outside_window', success=False, collected=False, mode='instagram'), clock)
    flag = security_flag(settings.inbox / 'security_halt.json')
    if flag:
        state.halt(flag)
        return _finish(state, _base('halted', flag, success=False, collected=False, mode='instagram'), clock)
    watchlist_path = settings.data_dir / 'watchlist.json'
    if not watchlist_path.exists():
        return _finish(state, _base('error', 'watchlist_missing', success=False, collected=False, mode='instagram'), clock)
    try:
        entries = load_watchlist(watchlist_path)
        path = _select_inbox(settings, inbox_file)
    except CollectError as error:
        return _finish(state, _base('error', error.reason, success=False, collected=False, mode='instagram'), clock)
    if path is None:
        return _finish(state, _base('no_batch', 'no_batch', success=False, collected=False, mode='instagram'), clock)
    if path.stat().st_size > 256_000:
        return _finish(state, _base('error', 'input_invalid', success=False, collected=False, mode='instagram'), clock)
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeError):
        return _finish(state, _base('error', 'input_invalid', success=False, collected=False, mode='instagram'), clock)
    if not lines or len(lines) > 100 or any(len(line) > 24_000 for line in lines):
        return _finish(state, _base('error', 'input_invalid', success=False, collected=False, mode='instagram'), clock)
    remaining = IG_READ_MAX - state.data['counts']['ig_reads']
    if len(lines) > remaining:
        return _finish(state, _base('stopped', 'ig_quota', success=False, collected=False, mode='instagram'), clock)
    documents, rejected, halt = parse_batch(lines, entries, clock)
    if halt:
        state.halt(halt)
        path.unlink()
        return _finish(state, _base('halted', halt, success=False, collected=False, mode='instagram'), clock)
    if not documents:
        path.unlink()
        reason = next(iter(rejected), 'input_invalid')
        return _finish(state, _base('done', reason, success=True, collected=False, mode='instagram', rejections=rejected), clock)
    if not credentials_present():
        return _finish(state, _base('error', 'missing_credentials', success=False, collected=False, mode='instagram'), clock)
    try:
        pages, claims = _extract(documents, kind='instagram', settings=settings, state=state, session=session, clock=clock)
    except CollectError as error:
        reason = error.reason if error.reason in RETRYABLE else 'runtime_error'
        return _finish(state, _base('partial', reason, success=False, collected=False, mode='instagram'), clock)
    path.unlink()
    short = max(0, 10 - state.data['counts']['ig_reads'])
    reason = 'ok' if not short else 'below_target'
    return _finish(state, _base('done', reason, success=True, collected=claims > 0, mode='instagram', pages=pages, rejections=rejected), clock)


def _review_result(report):
    """Keep deterministic IDs usable while redacting external evidence strings."""
    from .ground import secrets

    identifier_keys = {'claim_id', 'claim_ids', 'newest_claim_ids', 'supporting_claim_ids',
                       'eligible_supporting_claim_ids', 'candidate_id', 'conflict_id',
                       'policy_fingerprint', 'assessment_fingerprint', 'assessment_digest', 'content_hash'}
    identifier_keys.update({'change_id', 'change_ids', 'observation_id', 'observation_ids',
                           'event_id', 'event_ids', 'old_observation_id', 'new_observation_id',
                           'predecessor_observation_id', 'predecessor_claim_id', 'source_observation_id',
                           'source_claim_id', 'target_fingerprint', 'event_digest', 'artifact_sha256',
                           'manifest_sha256', 'latest_change_ids'})

    def clean(value, key=None):
        if isinstance(value, str):
            if value.startswith(('https://', 'http://')):
                try:
                    parsed = urlsplit(value)
                    if parsed.username or parsed.password:
                        return '[redacted_url]'
                except ValueError:
                    return '[redacted_url]'
            return value if key in identifier_keys and re.fullmatch(r'[0-9a-f]{64}', value) else strip_pii(value)
        if isinstance(value, list):
            return [clean(item, key) for item in value]
        if isinstance(value, dict):
            return {name: clean(item, name) for name, item in value.items()}
        return value

    blob = json.dumps(clean(report), ensure_ascii=False)
    for secret in secrets():
        blob = blob.replace(secret, '[redacted]')
    return json.loads(blob)


def _review_error(reason):
    return {'state': 'error', 'reason': reason, 'claims': [], 'conflicts': [],
            'fact_candidates': [], 'rights': 'internal_review_only', 'auto_publish_to_site': False}


def _assess_review(db, policy, clock):
    from .promotion import assess
    from .store import read_claims, stored_candidates
    return assess(read_claims(db), stored_candidates(db), policy, now=kst_now(clock))


def run_report(settings, *, policy_path=None, clock=None):
    """Read-only, including missing databases and pre-provenance schemas."""
    from .promotion_policy import load_policy
    from .store import connect_readonly
    clock = clock or Clock()
    try:
        policy = load_policy(policy_path, today=kst_now(clock).date())
        db = connect_readonly(settings.data_dir)
        try:
            if db:
                db.execute('BEGIN')
            report = _assess_review(db, policy, clock)
            from .changes import change_report
            from .change_feeds import manifest_status
            changes = change_report(db)
            changes['manifest'] = manifest_status(settings.data_dir, expected_watermark=changes['events'])
            report['changes'] = changes
        finally:
            if db:
                db.close()
        return _review_result({**report, 'state': 'ok', 'at': kst_now(clock).isoformat()})
    except CollectError as error:
        return _review_error(error.reason)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return _review_error('state_corrupt')


def run_promote(settings, *, policy_path, dry_run=False, clock=None):
    """Explicit local review gate. Never touches active/twin state or sources."""
    from .promotion_policy import load_policy
    from .store import persist_candidates
    clock = clock or Clock()
    if dry_run:
        return {**run_report(settings, policy_path=policy_path, clock=clock), 'dry_run': True}
    try:
        policy = load_policy(policy_path, today=kst_now(clock).date())
        _prepare_dir(settings.data_dir)
        with ProcessLock(settings.data_dir / 'daemon.lock'):
            db = claim_connect(settings.data_dir)
            try:
                db.execute('BEGIN IMMEDIATE')
                report = _assess_review(db, policy, clock)
                changed = persist_candidates(db, report['fact_candidates'], at=kst_now(clock).isoformat())
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
        return _review_result({**report, 'state': 'ok', 'dry_run': False,
                               'changed_candidates': changed, 'at': kst_now(clock).isoformat()})
    except CollectorBusy:
        return _review_error('already_running')
    except CollectError as error:
        return _review_error(error.reason)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return _review_error('state_corrupt')


def run_status(settings, *, clock=None):
    clock = clock or Clock()
    status_path = settings.data_dir / 'status.json'
    if not status_path.exists() or status_path.is_symlink():
        return public_result(_base('idle', 'idle', success=False, collected=False, mode='status'))
    try:
        if status_path.stat().st_size > 1_000_000:
            raise ValueError
        data = json.loads(status_path.read_bytes().decode())
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return public_result(_base('error', 'state_corrupt', success=False, collected=False, mode='status'))
    data['mode'] = 'status'
    data['at'] = data.get('at') or kst_now(clock).isoformat()
    return public_result(data)


def load_settings(**kwargs):
    return CollectSettings(**kwargs)


def _change_targets(settings, targets_path=None, *, testing=False, clock=None):
    from .targets import parse_targets
    if not targets_path:
        assembled = assemble_targets(settings, now=kst_now(clock), testing=testing, include_watchlist=False)
        if assembled['targets_status'] == 'invalid' or assembled['db_status'] != 'ok':
            return None
        return assembled['authorization']
    try:
        return parse_targets(Path(targets_path), testing=testing)
    except CollectError:
        return None


def _change_cycle(settings, *, targets_path=None, testing=False, export=True, rebaseline=False, clock=None):
    """Caller holds daemon.lock. Side failures never invalidate collection."""
    from . import changes
    from .change_feeds import export_changes
    db = None
    try:
        db = claim_connect(settings.data_dir)
        targets = _change_targets(settings, targets_path, testing=testing, clock=clock)
        if changes.needs_rebaseline(db) and not rebaseline:
            result = changes.process_pending(db, targets, testing=testing)
        elif rebaseline:
            result = changes.rebaseline(db, targets, testing=testing)
        else:
            changes.bootstrap_baselines(db, targets, testing=testing)
            result = changes.process_pending(db, targets, testing=testing)
        if export:
            exported = export_changes(db, settings.data_dir)
            result['export'] = exported
            if exported['reason'] == 'export_failed':
                result['reason'] = 'export_failed'
        return result
    except Exception:
        return {'reason': 'detector_error'}
    finally:
        if db:
            db.close()


def run_detect_changes(settings, *, dry_run=False, rebaseline=False):
    from .changes import change_report, rebaseline_candidates
    from .store import connect_readonly
    if dry_run:
        db = connect_readonly(settings.data_dir)
        try:
            result = change_report(db)
            if rebaseline:
                result['would_reseed'] = len(rebaseline_candidates(db, _change_targets(settings)))
            return _review_result({**result, 'state': 'ok', 'dry_run': True,
                                   'would_rebaseline': rebaseline,
                                   'rights': 'internal_review_only', 'auto_publish_to_site': False})
        finally:
            if db:
                db.close()
    try:
        _prepare_dir(settings.data_dir)
        with ProcessLock(settings.data_dir / 'daemon.lock'):
            result = _change_cycle(settings, rebaseline=rebaseline)
        return _review_result({**result, 'state': 'ok' if result['reason'] in ('ok', 'no_events') else 'error',
                               'rights': 'internal_review_only', 'auto_publish_to_site': False})
    except CollectorBusy:
        return _review_error('already_running')
    except (CollectError, OSError, ValueError, sqlite3.Error):
        return _review_error('state_corrupt')


def run_export_changes(settings, *, as_of=None):
    from .change_feeds import export_changes
    path = settings.data_dir / 'collector.sqlite3'
    if not path.exists():
        return {'state': 'ok', 'reason': 'no_events', 'rights': 'internal_review_only', 'auto_publish_to_site': False}
    try:
        _prepare_dir(settings.data_dir)
        with ProcessLock(settings.data_dir / 'daemon.lock'):
            db = claim_connect(settings.data_dir)
            try:
                result = export_changes(db, settings.data_dir, as_of=as_of)
            finally:
                db.close()
        return _review_result({**result, 'state': 'error' if result['reason'] == 'export_failed' else 'ok',
                               'rights': 'internal_review_only', 'auto_publish_to_site': False})
    except CollectorBusy:
        return _review_error('already_running')
    except (CollectError, OSError, ValueError, sqlite3.Error):
        return _review_error('state_corrupt')
