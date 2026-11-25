"""Run continuous, internal-only seed discovery through the active collector."""
from collections import Counter
from contextlib import nullcontext
from dataclasses import replace
import sqlite3
from uuid import uuid4

from . import checkpoints, datago
from .candidates import Candidates, Observation, TRANSITIONS
from .search import Tavily
from .web_seeds import WebSeeds
from ..crawl_policy import DISCOVERY
from ..fetcher import Fetcher
from ..util.timeutil import Clock, kst_now
from ..util.urls import registrable_domain
from ..locks import AlreadyRunning, RunLock
from ..web_proxy import resolve_run

SOURCES = ('licenses', 'directories', 'sitemaps', 'tavily')
SOURCE_ALIASES = {'datago': 'licenses', 'search': 'tavily'}
_PRIORITY = frozenset({'sauna', 'spa', 'jjimjilbang', 'hotel_spa', 'boutique_sauna', 'crossfit_box', 'hyrox_box'})
_REGIONS = ('서울', '부산', '대구', '인천', '광주', '대전', '울산', '세종', '경기',
            '강원', '충북', '충남', '전북', '전남', '경북', '경남', '제주')
_TOPICS = ('사우나 찜질방', '부티크 사우나 · 호텔 사우나', '크로스핏', '하이록스',
           '피트니스 체육관', '웰니스 행사')


class TavilyRun:
    def __init__(self, max_calls=None, heartbeat=None):
        self.max_calls = max_calls
        self.calls = 0
        self.heartbeat = heartbeat
        self.reason = ''

    def search_allowed(self):
        return not self.reason and (self.max_calls is None or self.calls < self.max_calls)

    def search_dispatch(self):
        if self.heartbeat is not None:
            self.heartbeat()
        self.calls += 1

    def stop_on(self, reason):
        if reason != 'ok':
            self.reason = reason


def _tavily(web, max_calls, transport):
    store, db, clock = web.store, web.db, web.clock
    run = TavilyRun(max_calls, heartbeat=web.heartbeat)
    provider = Tavily(web.getter('tavily'), db, clock=clock, context=run,
                      transport=transport, public_source=True, host_dir=web.fetcher.settings.data_dir)
    counts = Counter()
    if not run.search_allowed():
        return counts, 'skipped:max_calls', run.calls

    progress = checkpoints.load(db, 'tavily', 'rotation')
    queries = [f'{region} {topic}' for region in _REGIONS for topic in _TOPICS]
    offset = progress.get('query', 0) % len(queries)
    resolution = progress.get('candidate', {})
    pending = {True: [], False: []}
    for row in db.execute("SELECT * FROM seed_candidates WHERE status='new' ORDER BY id"):
        pending[row['category'] in _PRIORITY].append(row)
    ordered = []
    for priority, rows in pending.items():
        key = str(priority)
        cursor = resolution.get(key, '')
        ordered.extend((key, row) for row in rows if row['id'] > cursor)
        ordered.extend((key, row) for row in rows if row['id'] <= cursor)
    candidates = iter(ordered)
    remaining_queries = len(queries)
    next_candidate = next(candidates, None)
    turn = progress.get('turn', 'candidate')
    while run.search_allowed() and (next_candidate or remaining_queries):
        resolve = bool(next_candidate and (turn == 'candidate' or not remaining_queries))
        if resolve:
            key, row = next_candidate
            query = ' '.join(part for part in (row['name'], row['sido'], row['sigungu'], '홈페이지') if part)
        else:
            query = queries[offset]
        before = run.calls
        hits = provider.search(query)
        if run.calls > before:
            with db:
                db.execute("INSERT INTO request_log(url,domain,ts,kind,outcome,adapter) VALUES (?,?,?,'api',?,'seeds:tavily')",
                           (provider.endpoint, registrable_domain(provider.endpoint), kst_now(clock).isoformat(),
                            provider.reason))
        if provider.reason not in ('ok', 'tavily_invalid_query'):
            run.stop_on(provider.reason)
            break
        if resolve:
            basis = Observation(row['name'], row['category'], 'tavily', '',
                                row['address'], row['sido'], row['sigungu'], candidate_id=row['id'])
            for hit in hits:
                item = web.resolve(replace(basis, source_url=hit.url), hit.url)
                if item.fetch_url:
                    with db:
                        counts.update(store.record(item))
            resolution[key] = row['id']
            next_candidate = next(candidates, None)
            turn = 'query'
        else:
            for hit in hits:
                counts.update(web.record_page(hit.url, 'tavily'))
            offset = (offset + 1) % len(queries)
            remaining_queries -= 1
            turn = 'candidate'
        with db:
            checkpoints.save(db, 'tavily', 'rotation',
                             {'query': offset, 'candidate': resolution, 'turn': turn}, clock)
    status = run.reason or ('max_calls' if not run.search_allowed() else 'ok')
    return counts, status, run.calls


def _summary(counts=(), *, state='ok', sources=None):
    counter = Counter(counts)
    return {'state': state, **{key: counter[key] for key in TRANSITIONS}, 'sources': sources or {}}


def discover(settings, db, *, sources=None, max_calls=None, clock=None, session=None, tavily_transport=None, admission=None):
    """The dedicated flock covers dispatch, checkpoints and candidate transactions."""
    selected = set(SOURCES if sources is None else (SOURCE_ALIASES.get(key, key) for key in sources))
    if selected.difference(SOURCES) or (max_calls is not None and max_calls < 0):
        raise ValueError('invalid_discovery_options')
    try:
        guard = (nullcontext(admission) if admission is not None else
                 RunLock(db, settings.data_dir, str(uuid4()), clock=clock,
                         filename='seeds-discover.lock', lease_key='seed_discovery'))
        with guard as lock:
            return _run(settings, db, selected, max_calls, clock, session, tavily_transport, lock)
    except AlreadyRunning:
        return _summary(state='already_running')


def _run(settings, db, selected, max_calls, clock, session, tavily_transport, lock):
    clock = clock or Clock()
    egress, blocked, _ = resolve_run(clock=clock, host_dir=settings.data_dir)
    if blocked:
        return _summary(state=blocked)
    fetcher = Fetcher(settings, db, clock, egress=egress, public_scope=True, policy=DISCOVERY)
    fetcher.facts_only = True
    if session is not None:
        fetcher.session.close()
        fetcher.session = session
    try:
        store = Candidates(db, fetcher.clock, profile=settings.profile, testing=settings.testing)
        next_heartbeat = fetcher.clock.monotonic() + 30

        def heartbeat():
            nonlocal next_heartbeat
            if fetcher.clock.monotonic() >= next_heartbeat:
                lock.heartbeat()
                next_heartbeat = fetcher.clock.monotonic() + 30

        web = WebSeeds(store, fetcher, heartbeat=heartbeat)
        adapters = {'licenses': lambda: datago.discover(store, web.getter('licenses'), web.resolve),
                    'directories': web.directories, 'sitemaps': web.sitemaps,
                    'tavily': lambda: _tavily(web, max_calls, tavily_transport)}
        stats, total = {}, Counter()
        for adapter in SOURCES:
            if adapter not in selected:
                continue
            lock.heartbeat()
            try:
                counts, status, *usage = adapters[adapter]()
            except (OSError, ValueError, TypeError, sqlite3.Error):
                counts, status, usage = Counter(), 'partial:adapter_error', []
            if adapter in web.failed_sources and status == 'ok':
                status = 'partial:page_request'
            stats[adapter] = {**{key: counts[key] for key in TRANSITIONS}, 'status': status}
            if usage:
                stats[adapter]['calls'] = usage[0]
            total.update(counts)
        result = _summary(total, sources=stats)
        if fallback := egress.public():
            result['egress'] = fallback
        return result
    finally:
        fetcher.close()
