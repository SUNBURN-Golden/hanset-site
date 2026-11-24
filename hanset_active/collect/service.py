"""Web runs and desktop Instagram ingestion. No browser session is read or launched."""
import json
import re
import sqlite3
from collections import OrderedDict
from contextlib import ExitStack, closing
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

from . import CollectError, runtime
from .config import IG_READ_MAX, SUBPAGE_MAX, CollectSettings, domain_allowed
from .deepseek import DeepSeek, credentials_present
from .ground import SUMMARY_INSTRUCTION, parse_items, secrets, summary_items
from .ingest import load_watchlist, parse_batch, security_flag, within_window
from .safety import venue_identity_safe
from .state import State
from .store import connect as claim_connect
from .store import (digest, record, prune_web_target_blocks, recent_web_target_block,
                    remember_web_target_block, remember_web_target_observation, validated_authorization, web_target_history)
from .targets import canonical_requested_url, fetch_url_allowed, resolve_jobs
from .web import classify_page, select_subpages
from .web_extraction import WebExtraction, batches_fit
from ..crawl_policy import COLLECTION
from .plan import assemble_targets, ensure_daily_plan, load_targets, local_target_order, order_targets
from .watchlist_proposals import inspect_page, write_proposals

RETRYABLE = frozenset({
    'model_timeout', 'model_transport', 'model_rate_limited', 'model_invalid',
    'model_unavailable', 'model_auth', 'model_input_limit', 'missing_credentials',
    'model_insufficient_balance',
})
PAGE_SAMPLE = 200


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
    for page in (result.get('pages') or [])[:PAGE_SAMPLE]:
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
        'pages': pages,
        'counts': result.get('counts') if isinstance(result.get('counts'), dict) else {},
        'budgets': result.get('budgets') if isinstance(result.get('budgets'), dict) else {},
    }
    if isinstance(result.get('at'), str):
        out['at'] = result['at']
    rejections = result.get('rejections')
    if isinstance(rejections, dict) and all(
            isinstance(key, str) and key.replace('_', '').isalnum() and type(value) is int and 0 <= value
            for key, value in rejections.items()):
        out['rejections'] = dict(rejections)
    providers = result.get('provider')
    if isinstance(providers, dict) and all(
            key in ('deepseek', 'qwen') and type(value) is int and 0 <= value
            for key, value in providers.items()):
        out['provider'] = dict(providers)
    if result.get('deepseek_skipped') == 'insufficient_balance':
        out['deepseek_skipped'] = 'insufficient_balance'
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
    for secret in secrets():
        blob = blob.replace(secret, '[redacted]')
    return json.loads(blob)


def _finish(state, body, clock):
    body['at'] = kst_now(clock).isoformat()
    body['counts'] = {**state.data['counts'], **body.get('counts', {})}
    body['budgets'] = dict(state.data['budgets'])
    body['provider'] = dict(state.model_providers)
    if state.deepseek_skipped == 'insufficient_balance':
        body['deepseek_skipped'] = 'insufficient_balance'
    if state.proxy_heal is not None:
        body['proxy_heal'] = dict(state.proxy_heal)
    clean = public_result(body)
    state.write_status(clean)
    return clean


def _prepare_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def _extract_summaries(rows, *, settings, state, session, qwen_session, clock):
    pages, claims = [], 0
    with (closing(claim_connect(settings.data_dir)) as db,
          DeepSeek(state, model=settings.model, session=session, qwen_session=qwen_session) as router):

        def commit(batch):
            if not batch:
                return
            answer = router.ask(summary_items(batch), instruction=SUMMARY_INSTRUCTION)
            try:
                parsed = parse_items(answer, batch)
            except CollectError:
                router.reject_last()
                raise
            for row, (grounded, _) in zip(batch, parsed):
                disposition, ids = record(
                    db, kind='instagram', url=row['url'], text_hash=digest(row['text']), disposition='applied',
                    claims=grounded, venue_id=row['venue_id'], venue_key=row['venue_key'],
                    seen_at=row['seen_at'], clock=clock)
                seen(row, disposition, len(ids))

        def seen(row, disposition, added=0):
            nonlocal claims
            state.data['counts']['ig_reads'] += 1
            state.data['counts']['claims'] += added
            state.complete(row['url'], disposition)
            claims += added
            pages.append({'url': row['url'], 'disposition': disposition})

        batch = []
        for row in rows:
            if db.execute(
                "SELECT 1 FROM receipts WHERE source_kind='instagram' AND source_url=? AND content_hash=?",
                (row['url'], digest(row['text']))).fetchone():
                seen(row, 'duplicate')
                continue
            if not batches_fit(summary_items(batch + [row]), SUMMARY_INSTRUCTION):
                commit(batch)
                batch = []
            batch.append(row)
        commit(batch)
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


def run_web(settings, *, targets_path=None, clock=None, session=None, qwen_session=None, search=None, testing=False,
            planner=None, planner_session=None, admission=None):
    clock = clock or Clock()
    mode = 'web'
    try:
        _prepare_dir(settings.data_dir)
        with ExitStack() as stack:
            if admission is None:
                stack.enter_context(ProcessLock(settings.data_dir / 'daemon.lock'))
                active = stack.enter_context(closing(active_connect(settings.active_dir / 'active.sqlite3')))
            else:
                active = admission.db
            recovery = _change_cycle(settings, targets_path=targets_path, testing=testing, export=False,
                                     clock=clock)
            try:
                result = _run_web(settings, targets_path=targets_path, clock=clock, session=session, search=search,
                                  testing=testing, planner=planner, planner_session=planner_session,
                                  qwen_session=qwen_session, active=active, facts_only=admission is not None)
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
    except (OSError, sqlite3.Error):
        return public_result(_base('error', 'state_corrupt', success=False, collected=False, mode=mode))


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


def _run_web(settings, *, targets_path, clock, session, search, testing, planner=None,
             planner_session=None, qwen_session=None, active, facts_only=False):
    state = State(settings.data_dir, clock, active_dir=settings.active_dir)
    with (closing(runtime.connect(settings.active_dir)) as ledger,
          DeepSeek(state, model=settings.model, session=session,
                   qwen_session=qwen_session) as router):
        return _drain_web(settings, state, ledger, router, targets_path=targets_path, clock=clock,
                          search=search, testing=testing, planner=planner, planner_session=planner_session, active=active,
                          facts_only=facts_only)


def _drain_web(settings, state, ledger, router, *, targets_path, clock, search, testing, planner, planner_session, active, facts_only):
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
                                 assembled=assembled, router=router)
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
    history = None
    skipped = {}
    deferred_urls = set()
    unsafe = 0
    egress = None
    extraction = None

    def finish_web(body):
        # Fetch/extraction results take precedence over cooldown notes in the
        # bounded public page list; the aggregate retains the full skip count.
        body['pages'].extend({'url': url, 'disposition': 'recent_block_skipped'} for url in skipped)
        rejections = dict(body.get('rejections') or {})
        if extraction is not None:
            body['counts'] = extraction.counts
            rejections.update(extraction.rejections)
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
        completed = runtime.web_completed(ledger, state, testing=testing)
        scheduled = {}
        waiting = {tuple(row) for row in ledger.execute(
            'SELECT requested_url,venue_id,venue_key FROM extraction_retries WHERE retry_at>?',
            (kst_now(clock).isoformat(),))} if facts_only else set()
        runtime_secrets = secrets()
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
            if not venue_identity_safe(job['venue_id'], job['venue_key'], runtime_secrets):
                unsafe += 1
                continue
            if cooling(url) or url in completed or (url, job['venue_id'], job['venue_key']) in waiting:
                continue
            if url in scheduled:
                if job['seed_url'] or not scheduled[url]['seed_url']:
                    continue
                # A manual or gap URL owns this fetch even if the seed ranked first.
                del scheduled[url]
            scheduled[url] = {**job, 'depth': 0}
        # A child discovered before a crash can be resumed using its currently
        # authorized parent, even when that parent already completed today.
        parents = {}
        for job in jobs:
            auth = job['source_authorization']
            if auth:
                parents[(auth['requested_url'], job['venue_id'], job['venue_key'])] = job
        for key, venue_id, venue_key, parent_url, fetch_url in ledger.execute(
                '''SELECT requested_url,venue_id,venue_key,parent_url,fetch_url
                   FROM extraction_retries WHERE retry_at<=? AND parent_url IS NOT NULL''',
                (kst_now(clock).isoformat(),)):
            if key in scheduled or key in completed or cooling(key):
                continue
            parent = parents.get((parent_url, venue_id, venue_key))
            if (parent and registrable_domain(key) == registrable_domain(parent_url)
                    and domain_allowed(key, parent['domains'])
                    and fetch_url_allowed(fetch_url, scope_domains={registrable_domain(key)}, testing=testing)
                    and canonical_requested_url(fetch_url, testing=testing) == key):
                scheduled[key] = {**parent, 'url': fetch_url, 'parent_url': parent_url, 'depth': 1,
                                  'source_authorization': None, 'discovered_subpage': True}
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
        if not pending and not deferred and skipped:
            return finish_web(_base('done', 'recent_block_skipped', success=True, collected=False, mode='web', pages=notes))
        if not pending and not deferred and jobs and unsafe == len(jobs):
            return finish_web(_base('stopped', 'unsafe_url_skipped', success=False, collected=False, mode='web', pages=notes))
        if not pending and not deferred and jobs:
            return finish_web(_base('already_complete', 'already_complete', success=True, collected=False, mode='web', pages=notes))
        if not pending and not deferred:
            reason = notes[0]['disposition'] if notes else 'no_urls'
            return finish_web(_base('stopped', reason, success=False, collected=False, mode='web', pages=notes))
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
        pages = list(notes[:PAGE_SAMPLE])
        proposal_evidence = []
        stop_reason = None

        def sample(url, disposition):
            if len(pages) < PAGE_SAMPLE:
                pages.append({'url': url, 'disposition': disposition})

        extraction = WebExtraction(history, ledger, state, router, sample)
        try:
            with ExitStack() as stack:
                if not facts_only:
                    stack.enter_context(RunLock(active, active_settings.data_dir, 'collect-web', clock=clock))
                fetcher = Fetcher(active_settings, active, clock, proxy=proxy, public_scope=True, policy=COLLECTION)
                if facts_only:
                    fetcher.facts_only = True
                try:
                    attempted, children = set(), {}
                    while pending or deferred:
                        job_key, job = (pending or deferred).popitem(last=False)
                        if cooling(job_key) or job_key in completed or job_key in attempted:
                            continue
                        attempted.add(job_key)
                        scope = job['scope_domains'] & {registrable_domain(job_key)}
                        url_allowed = partial(fetch_url_allowed, scope_domains=scope, testing=testing)
                        fetch_options = {'preserve_path': True} if job['discovery_url'] else {}
                        fetched = fetcher.fetch(job['url'], subpage=bool(job.get('discovered_subpage')),
                                                url_allowed=url_allowed, **fetch_options)
                        proxy_outage = fetched.outcome == 'error' and fetched.reason == 'proxy_unavailable'
                        if proxy_outage or getattr(fetcher, 'proxy_failed', False) is True:
                            egress = {'mode': 'direct', 'reason': 'proxy_unavailable', 'phase': 'mid_run'}
                        if fetched.outcome == 'cap_reached':
                            stop_reason = 'active_body_cap'
                            break
                        seen_at = kst_now(clock).isoformat()
                        state.data['counts']['pages'] += 1
                        state.save()
                        disposition, text = classify_page(fetched, scope)
                        try:
                            final = canonical_requested_url(fetched.url or job['url'], testing=testing)
                        except ValueError:
                            final = job_key
                            disposition, text = 'denied', None
                        auth = job['source_authorization']
                        authorization = validated_authorization(
                            job['url'], final, {**auth, 'final_url': final} if auth and
                            registrable_domain(final) in scope else None, job['domains'], testing=testing)
                        remember_web_target_block(history, job['url'], disposition,
                                                  status=fetched.status, clock=clock, testing=testing)
                        if disposition != 'ready':
                            remember_web_target_observation(
                                history, job['url'], disposition, content_hash=fetched.content_hash or None,
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
                                if (child_key in completed or child_key in attempted or cooling(child_key)
                                        or (child_key, *target_key) in waiting):
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
                                             'parent_url': job_key, 'discovered_subpage': True}
                                    runtime.retry_web(ledger, {**child, 'requested_url': child_key},
                                                      clock, 'pending_fetch')
                                queue[child_key] = {**child, 'depth': 1}
                                if queue is deferred:
                                    deferred_urls.add(child_key)
                                else:
                                    follow.append(child_key)
                            for child_key in reversed(follow):
                                pending.move_to_end(child_key, last=False)
                            children[target_key] = children.get(target_key, 0) + len(selected)
                        if disposition != 'ready':
                            if facts_only and disposition == 'error' and fetched.status not in (404, 410):
                                runtime.retry_web(ledger, {**job, 'requested_url': job_key},
                                                  clock, 'transport_retry', delay=600)
                                stop_reason = 'transport_retry_pending'
                            elif not proxy_outage:
                                runtime.complete_web(ledger, state.data['day'], job_key, disposition)
                            sample(job['url'], disposition)
                            continue
                        row = {**job, 'requested_url': job_key, 'source_authorization': authorization,
                               'source_type': 'own_site' if authorization else 'third_party',
                               'text': text, 'seen_at': seen_at, 'final_url': final, '_testing': testing,
                               '_facts_only': facts_only}
                        proposal_evidence.extend(inspect_page(
                            row, fetched, observed_at=seen_at, disposition=disposition))
                        extraction.offer(row)
                    extraction.flush()
                finally:
                    fetcher.close()
        except ActiveBusy:
            return finish_web(_base('deferred', 'active_busy', success=False, collected=False,
                                    mode='web', pages=pages))
        if proposal_evidence:
            try:
                write_proposals(settings, proposal_evidence, day=kst_now(clock).date().isoformat())
            except (CollectError, OSError):
                # Proposals are a side artifact; never abort extraction of pages
                # that were already fetched and passed every guard.
                pages.append({'url': '', 'disposition': 'proposals_invalid'})
        state.save()
        reason = stop_reason or extraction.reason
        return finish_web(_base('partial' if reason else 'done', reason or 'ok', success=reason is None,
                                collected=extraction.added > 0, mode='web', pages=pages))
    except (sqlite3.Error, ValueError, TypeError):
        raise CollectError('state_corrupt') from None
    finally:
        if history is not None:
            history.close()


def run_ingest(settings, *, inbox_file=None, clock=None, session=None, qwen_session=None):
    clock = clock or Clock()
    mode = 'instagram'
    try:
        _prepare_dir(settings.data_dir)
        _prepare_dir(settings.inbox)
        with ProcessLock(settings.data_dir / 'daemon.lock'):
            return _run_ingest(settings, inbox_file=inbox_file, clock=clock, session=session, qwen_session=qwen_session)
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


def _run_ingest(settings, *, inbox_file, clock, session, qwen_session):
    state = State(settings.data_dir, clock, active_dir=settings.active_dir)
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
        pages, claims = _extract_summaries(documents, settings=settings, state=state, session=session,
                                          qwen_session=qwen_session, clock=clock)
    except CollectError as error:
        reason = error.reason if error.reason in RETRYABLE else 'runtime_error'
        return _finish(state, _base('partial', reason, success=False, collected=False, mode='instagram'), clock)
    path.unlink()
    short = max(0, 10 - state.data['counts']['ig_reads'])
    reason = 'ok' if not short else 'below_target'
    return _finish(state, _base('done', reason, success=True, collected=claims > 0, mode='instagram', pages=pages, rejections=rejected), clock)


def _review_result(report):
    """Keep deterministic IDs usable while redacting external evidence strings."""
    identifier_keys = {'venue_id', 'claim_id', 'claim_ids', 'newest_claim_ids', 'supporting_claim_ids',
                       'eligible_supporting_claim_ids', 'current_supporting_claim_ids', 'candidate_id', 'conflict_id',
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
            'fact_candidates': [], 'facts': [], 'rights': 'internal_review_only', 'auto_publish_to_site': False}


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
