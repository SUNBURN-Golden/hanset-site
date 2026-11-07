"""Web runs and desktop Instagram ingestion. No browser session is read or launched."""
import json
from pathlib import Path

from hanset_active.config import Settings
from hanset_active.db import connect as active_connect
from hanset_active.fetcher import Fetcher
from hanset_active.locks import AlreadyRunning as ActiveBusy
from hanset_active.locks import RunLock
from hanset_active.util.pii import strip_pii
from hanset_active.util.timeutil import Clock, kst_now
from hanset_twin.daemon import AlreadyRunning as CollectorBusy
from hanset_twin.daemon import ProcessLock

from . import CollectError
from .config import IG_READ_MAX, PAGE_DAY_CAP, PAGE_RUN_CAP, CollectSettings
from .deepseek import OUTPUT_TOKENS, DeepSeek, credentials_present
from .ground import parse_items, payload_items
from .ingest import load_watchlist, parse_batch, security_flag, within_window
from .state import State
from .store import connect as claim_connect
from .store import digest, record
from .targets import parse_targets, resolve_jobs
from .web import classify_page

RETRYABLE = frozenset({
    'model_timeout', 'model_transport', 'model_rate_limited', 'model_invalid',
    'model_unavailable', 'model_auth', 'model_budget_exhausted',
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
    blob = json.dumps(out, ensure_ascii=False)
    from .ground import secrets
    for secret in secrets():
        blob = blob.replace(secret, '[redacted]')
    return json.loads(blob)


def _finish(state, body, clock):
    body['at'] = kst_now(clock).isoformat()
    body['counts'] = dict(state.data['counts'])
    body['budgets'] = dict(state.data['budgets'])
    clean = public_result(body)
    state.write_status(clean)
    return clean


def _prepare_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def _chunks(rows):
    batches, current = [], []
    for row in rows:
        trial = current + [row]
        encoded = payload_items([{'text': item['text']} for item in trial])
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
                seen_at=row['seen_at'], clock=clock)
        if kind == 'web':
            state.complete(row['url'], disposition)
        else:
            state.data['counts']['ig_reads'] += 1
            state.complete(row['url'], disposition)
        state.data['counts']['claims'] += len(ids)
        new_claims += len(ids)
        state.save()
        pages.append({'url': row['url'], 'disposition': disposition})
    return pages, new_claims


def _extract(rows, *, kind, settings, state, session, clock):
    db = claim_connect(settings.data_dir)
    pages, claims = [], 0
    brain = DeepSeek(state, model=settings.model, session=session)
    try:
        for batch in _chunks(rows):
            fresh = []
            for row in batch:
                text_hash = digest(row['text'])
                if db.execute(
                        'SELECT 1 FROM receipts WHERE source_kind=? AND source_url=? AND content_hash=?',
                        (kind, row['url'], text_hash)).fetchone():
                    if kind == 'instagram':
                        state.data['counts']['ig_reads'] += 1
                    state.complete(row['url'], 'duplicate')
                    pages.append({'url': row['url'], 'disposition': 'duplicate'})
                else:
                    fresh.append(row)
            if not fresh:
                continue
            if not state.can_model(1, OUTPUT_TOKENS):
                raise CollectError('model_budget_exhausted')
            answer = brain.ask(payload_items([{'text': row['text']} for row in fresh]))
            parsed = parse_items(answer, [{'text': row['text']} for row in fresh])
            stored, added = _store_batch(db, fresh, parsed, kind=kind, clock=clock, state=state)
            pages.extend(stored)
            claims += added
    finally:
        brain.close()
        db.close()
    return pages, claims


def _default_search():
    from hanset_active.search import discover_known_venue
    return discover_known_venue


def run_web(settings, *, targets_path=None, clock=None, session=None, search=None, testing=False):
    clock = clock or Clock()
    mode = 'web'
    try:
        _prepare_dir(settings.data_dir)
        with ProcessLock(settings.data_dir / 'daemon.lock'):
            return _run_web(settings, targets_path=targets_path, clock=clock, session=session, search=search, testing=testing)
    except CollectorBusy:
        return public_result(_base('already_running', 'already_running', success=False, collected=False, mode=mode))
    except CollectError as error:
        return public_result(_base('error', error.reason, success=False, collected=False, mode=mode))


def _run_web(settings, *, targets_path, clock, session, search, testing):
    state = State(settings.data_dir, clock)
    path = Path(targets_path) if targets_path else settings.data_dir / 'web_targets.json'
    if targets_path:
        from .config import outside
        path = outside(path)
    if not path.exists():
        return _finish(state, _base('no_targets', 'no_targets', success=False, collected=False, mode='web'), clock)
    try:
        targets = parse_targets(path, testing=testing)
    except CollectError as error:
        return _finish(state, _base('error', error.reason, success=False, collected=False, mode='web'), clock)
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
    try:
        needs_search = any(item['tavily'] and not item['urls'] for item in targets)
        jobs, notes = resolve_jobs(
            targets,
            search=search if search is not None or not needs_search else _default_search(),
            db=active,
            clock=clock,
            testing=testing,
        )
        pending = [job for job in jobs if job['url'] not in state.data['completed']]
        if not pending and jobs:
            return _finish(state, _base('already_complete', 'already_complete', success=True, collected=False, mode='web', pages=notes), clock)
        if not pending:
            reason = notes[0]['disposition'] if notes else 'no_urls'
            return _finish(state, _base('stopped', reason, success=False, collected=False, mode='web', pages=notes), clock)
        if state.data['counts']['pages'] >= PAGE_DAY_CAP:
            return _finish(state, _base('stopped', 'daily_cap', success=False, collected=False, mode='web', pages=notes), clock)
        if not credentials_present():
            return _finish(state, _base('error', 'missing_credentials', success=False, collected=False, mode='web', pages=notes), clock)
        if not state.can_model(1, OUTPUT_TOKENS):
            return _finish(state, _base('stopped', 'model_budget_exhausted', success=False, collected=False, mode='web', pages=notes), clock)
        ready, pages = [], list(notes)
        stop_reason = None
        try:
            with RunLock(active, active_settings.data_dir, 'collect-web', clock=clock):
                fetcher = Fetcher(active_settings, active, clock)
                try:
                    run_pages = 0
                    for job in pending:
                        if state.data['counts']['pages'] >= PAGE_DAY_CAP:
                            stop_reason = 'daily_cap'
                            break
                        if run_pages >= PAGE_RUN_CAP:
                            stop_reason = 'run_cap'
                            break
                        fetched = fetcher.fetch(job['url'])
                        if fetched.outcome == 'cap_reached':
                            stop_reason = 'active_body_cap'
                            break
                        state.data['counts']['pages'] += 1
                        run_pages += 1
                        state.save()
                        disposition, text = classify_page(fetched, job['domains'])
                        if disposition != 'ready':
                            state.complete(job['url'], disposition)
                            pages.append({'url': job['url'], 'disposition': disposition})
                            continue
                        ready.append({**job, 'text': text, 'seen_at': kst_now(clock).isoformat()})
                finally:
                    fetcher.close()
        except ActiveBusy:
            return _finish(state, _base('deferred', 'active_busy', success=False, collected=False, mode='web', pages=pages), clock)
        extracted, claims = [], 0
        if ready:
            try:
                extracted, claims = _extract(ready, kind='web', settings=settings, state=state, session=session, clock=clock)
            except CollectError as error:
                pages.extend(extracted)
                reason = error.reason if error.reason in RETRYABLE else 'runtime_error'
                return _finish(state, _base('partial', reason, success=False, collected=claims > 0, mode='web', pages=pages), clock)
        pages.extend(extracted)
        if stop_reason == 'run_cap':
            return _finish(state, _base('done', 'run_cap', success=True, collected=claims > 0, mode='web', pages=pages), clock)
        if stop_reason:
            return _finish(state, _base('partial', stop_reason, success=False, collected=claims > 0, mode='web', pages=pages), clock)
        return _finish(state, _base('done', 'ok', success=True, collected=claims > 0, mode='web', pages=pages), clock)
    finally:
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
    return _finish(state, _base('done', reason, success=True, collected=claims > 0, mode='instagram', pages=pages), clock)


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
