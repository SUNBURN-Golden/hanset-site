"""One identity, no bypasses: guarded HTTP with persistent budgets and robots."""
from contextlib import contextmanager
from dataclasses import dataclass, field
import threading
from urllib.parse import urlsplit, urljoin, parse_qsl, urlencode, unquote
import re
import logging
import requests
from bs4 import BeautifulSoup
from .config import Settings
from .db import consume_budget, deterministic_id
from .crawl_policy import LEGACY, KoreanNewsRobots, is_korean_news
from .host_policy import BACKOFF_STATUSES, HostPolicy, origin
from .locks import host_lock, hostname
from .util.timeutil import Clock, kst_now, kst_day
from .util.urls import (canonicalize, registrable_domain, is_collect_denied, is_denied, is_excluded,
                        literal_private, same_fetch_target)
from .util.pii import strip_pii
from .web_proxy import Egress, configured, is_heavy
from .util.hashing import content_hash

USER_AGENT='HansetActiveBot/1.0 (+mailto:hanset@soulbound.co.kr)'
BLOCK_STATUSES = (401, 403, 429, 202, 402, 407, 451)
PROXY_RETRY_DIRECT = "proxy_retry_direct"
_CREDENTIAL_QUERY = frozenset({
    'servicekey', 'service_key', 'apikey', 'api_key', 'key', 'password', 'token',
    'access_token', 'secret', 'session', 'sid', 'auth', 'authorization', 'sig', 'signature',
})


def _safe_public_query(url):
    return not any(key.casefold() in _CREDENTIAL_QUERY for key, _ in parse_qsl(urlsplit(unquote(url)).query))


def _safe_transport_log(record):
    # urllib3 debug messages can otherwise include the outbound license key.
    return not re.search(r'[?&]serviceKey=', record.getMessage(), re.I)


logging.getLogger('urllib3.connectionpool').addFilter(_safe_transport_log)


@dataclass
class FetchResult:
    url: str
    outcome: str
    status: int | None = None
    content: bytes = b''
    content_type: str = ''
    reason: str = ''
    fetched_at: str = ''
    content_hash: str = ''
    fetch_method: str = 'http'
    source_ref: dict = field(default_factory=dict)
    extractor_version: str = '1.0.0'
    retry_after: str | None = None
    # Persistence uses the caller's URL normalization when it differs from the fetch URL.
    identity_url: str = ''
    # Transient link metadata for one-hop discovery before body sanitization.
    links: list[tuple[str, str]] | None = None
    @property
    def text(self):
        return self.content.decode('utf-8',errors='replace')
    @property
    def ok(self):
        return self.outcome=='ok'


def detect_outcome(content: bytes, content_type: str, status: int, *, allow_short=False) -> str:
    if status in BLOCK_STATUSES:
        return 'blocked'
    if status>=400:
        return 'error'
    if 'html' not in content_type and content.strip():
        if content_type.startswith('text/') and re.match(
                rb'\s*(?:access denied|just a moment|captcha|verify you are human|subscribers only|subscription required)', content[:256], re.I):
            return 'blocked'
        return 'ok'
    soup=BeautifulSoup(content,'lxml')
    text=soup.get_text(' ',strip=True)
    if re.search(r'just a moment|verify you are human|captcha|access.denied|select all squares|anomaly|bot.challenge',text,re.I):
        return 'blocked'
    if soup.select_one('input[type="password"]') or (len(text)<500 and re.search(r'로그인|sign in|log in|cookie (?:wall|consent)',text,re.I)):
        return 'login_wall'
    if re.search(r'\b(?:subscribers only|members only|subscription (?:is )?required)\b|'
                 r'\bsubscribe to (?:read|continue|view|access)\b|'
                                  r'구독자 전용|구독하시면.{0,24}(?:읽|볼 수)',text,re.I):
        return 'blocked'
    from .extract.html import extract
    main=extract(content)['text']
    if (len(main)<80 and not (allow_short and (main or soup.title))) or re.search(r'\b(?:page not found|404 not found)\b|페이지를 찾을 수 없',main,re.I):
        return 'empty_body'
    return 'ok'

class Fetcher:
    def __init__(self, settings: Settings | None = None, db=None, clock: Clock | None = None, proxy: str = '', *, public_scope=False, policy=LEGACY, egress: Egress | None = None):
        self.policy=policy
        self.run_id = None
        self.settings=settings or Settings()
        self.public_scope=public_scope
        self.discover_links = False
        if egress is None:
            proxy = configured({'HANSET_WEB_PROXY': proxy})
            egress = Egress(mode='proxy', proxy=proxy) if proxy else Egress()
        self.egress = egress
        if db is None:
            raise ValueError('fetch_audit_required')
        self.db = db
        self._host_policy = None
        self.clock=clock or Clock()
        self.session=requests.Session()
        self.session.trust_env=False
        self.session.headers.update({'User-Agent':USER_AGENT,'Accept-Encoding':'gzip'})
        if request_proxy := self.egress.request_proxy():
            self.session.proxies.update({'http': request_proxy, 'https': request_proxy})
        self._locks={}
        self._guard=threading.RLock()
        self._delay={}
        self._stopped={}
    @contextmanager
    def _lock(self, url):
        host = hostname(url)
        with self._guard:
            local = self._locks.setdefault(host, threading.RLock())
        with local, host_lock(self.settings.data_dir, host):
            yield

    @property
    def host_policy(self):
        with self._guard:
            if self._host_policy is None:
                self._host_policy = HostPolicy(self.settings.data_dir, self.clock, self.settings.min_interval)
            return self._host_policy
    def _reason_denied(self,url, *, registration=False):
        if self.public_scope and (not registration or getattr(self, 'facts_only', False)):
            if is_collect_denied(url):
                return 'denied host'
        else:
            if is_denied(url,self.settings.profile['denied_domains']) or is_denied(url):
                return 'denied host'
            if is_excluded(url, unquote(url)):
                return 'excluded'
        if literal_private(url):
            host=urlsplit(url).hostname
            if not (self.settings.testing and self.settings.allow_private_hosts and host in ('127.0.0.1','::1','localhost')):
                return 'private literal host'
        return ''
    def _unsafe_public_url(self, url, kind):
        if not getattr(self, 'facts_only', False):
            return False
        from .collect.safety import evidence_rejection
        # API query strings can contain a venue query. The URL stored in the
        # request ledger is restricted to the endpoint in facts-only mode.
        return bool(evidence_rejection(unquote(url.split('?', 1)[0] if kind == 'api' else url)))

    def _audit_url(self, url, kind, *, preserve_encoding=False):
        if kind == 'api' and getattr(self, 'facts_only', False):
            url = url.split('?', 1)[0]
        if preserve_encoding or self.public_scope:
            decoded = unquote(url)
            if strip_pii(decoded) == decoded:
                return url
        return strip_pii(url)

    def _resolve_taskless_errors(self, *urls):
        candidates = set(urls)
        for url in urls:
            try:
                candidates.add(canonicalize(url))
            except (ValueError, UnicodeError):
                pass
        with self._guard, self.db:
            self.db.executemany(
                "UPDATE failures SET state='resolved' "
                "WHERE failure_id=? AND task_id IS NULL AND reason='error' AND state='pending'",
                ((deterministic_id(self._audit_url(url, 'body'), None, 'error'),)
                 for url in candidates))

    def _stopped_reason(self, url):
        with self._guard:
            reason = self._stopped.get(hostname(url), '')
            persistent, retry = self.host_policy.stopped(url)
            return reason or persistent, retry

    def _wait(self, url):
        if not is_korean_news(url):
            return
        with self._guard:
            delay = self.host_policy.delay(url, self._delay.get(hostname(url), 0))
        self.clock.sleep(delay)
        with self._guard:
            self.host_policy.dispatch(url)

    def register_candidate(self,url: str) -> bool:
        try:
            url=canonicalize(url)
        except ValueError:
            return False
        if self._reason_denied(url,registration=True):
            return False
        with self._guard:
            # tasks' unique dedupe key makes registration and budget use atomic.
            with self.db:
                if self.db.execute('SELECT 1 FROM tasks WHERE dedupe_key=?',(url,)).fetchone():
                    return False
                day=kst_day(self.clock)
                used=self.db.execute("SELECT used FROM budgets_daily WHERE day=? AND kind='candidates'",(day,)).fetchone()
                if (used[0] if used else 0)>=self.settings.candidate_cap:
                    return False
                self.db.execute("INSERT INTO budgets_daily VALUES (?,'candidates',1) ON CONFLICT(day,kind) DO UPDATE SET used=used+1",(day,))
                self.db.execute("INSERT INTO tasks(task_id,dedupe_key,url,domain,task_class,kind,priority,novelty,source_quality,created_at,updated_at) VALUES (?,?,?,?,'discover','trial',0.35,1,0.5,?,?)",(deterministic_id(url),url,url,registrable_domain(url),kst_now(self.clock).isoformat(),kst_now(self.clock).isoformat()))
                return True
    def _inaccessible(self,domain,reason):
        self._stopped[domain]=reason
        with self._guard, self.db:
            self.db.execute("INSERT INTO sources(source_id,domain,state,reason) VALUES (?,?,'inaccessible',?) ON CONFLICT(domain) DO UPDATE SET state='inaccessible',reason=excluded.reason",(deterministic_id(domain),domain,reason))
    def _refuse(self, url, reason):
        with self._guard:
            self.host_policy.refuse(url)
            if is_korean_news(url):
                self._inaccessible(hostname(url), reason)

    def _request(self,url,kind,headers=None, *, service_key=None, audit_source=None):
        domain=registrable_domain(url)
        with self._lock(url):
            stopped, _ = self._stopped_reason(url)
            if stopped:
                return None, b'', stopped, None
            with self._guard:
                cap=self.settings.aux_cap if kind!='body' else self.settings.body_cap
                if self.policy.use_daily_budgets and not consume_budget(self.db,'aux' if kind!='body' else 'bodies',cap,self.clock):
                    return None,b'', 'cap_reached', None
            self._wait(url)
            status=None
            body=b''
            response=None
            reason=''
            transfer_complete = False
            outcome=None
            request_proxy = self.egress.request_proxy()
            try:
                outbound=url + (("&" if urlsplit(url).query else "?") + urlencode({"serviceKey": service_key}) if service_key else "")
                # Per-dispatch proxies keep the snapshot stable during fallback on another host.
                proxies = dict.fromkeys(('http', 'https'), request_proxy or None)
                response=self.session.get(outbound,proxies=proxies,headers={**(headers or {}), 'User-Agent': USER_AGENT},allow_redirects=False,timeout=(self.settings.timeout,self.settings.timeout),stream=True)
                status=response.status_code
                # Refusal headers take precedence over body reads and egress fallback.
                host = hostname(url)
                news = is_korean_news(url)
                with self._guard:
                    if status in BACKOFF_STATUSES:
                        self.host_policy.block(url, response.headers.get('Retry-After', ''))
                        self._stopped[host] = 'host_backoff'
                if status in BLOCK_STATUSES and (news or status not in BACKOFF_STATUSES):
                    self._refuse(url, 'blocked')
                ct=response.headers.get('Content-Type','').lower()
                limit=8*1024**2 if 'pdf' in ct else 4*1024**2 if ct.startswith('image/') else 2*1024**2
                if status in (*BLOCK_STATUSES, *BACKOFF_STATUSES):
                    pass
                elif request_proxy and is_heavy(ct):
                    reason='heavy resource'
                elif int(response.headers.get('Content-Length','0'))>limit:
                    reason='size limit'
                else:
                    for chunk in response.iter_content(65536):
                        body+=chunk
                        if len(body)>limit:
                            body=b''
                            reason='size limit'
                            break
                    else:
                        transfer_complete = True
                if (kind != 'robots' and not service_key and not reason
                        and not 300 <= status < 400 and status not in (*BLOCK_STATUSES, *BACKOFF_STATUSES)):
                    outcome = detect_outcome(body, ct, status,
                                             allow_short=self.policy.allow_short)
                    if outcome in ('blocked', 'login_wall'):
                        self._refuse(url, outcome)
            except (requests.RequestException, ValueError) as exc:
                reason = type(exc).__name__
                transport_error = isinstance(exc, (requests.ConnectionError, requests.Timeout,
                                                   requests.exceptions.ChunkedEncodingError,
                                                   requests.exceptions.ContentDecodingError))
                if transport_error and body and kind != 'robots' and not service_key and 200 <= status < 300:
                    partial_outcome = detect_outcome(body, ct, status)
                    if partial_outcome in ('blocked', 'login_wall'):
                        outcome, reason = partial_outcome, ''
                        self._refuse(url, outcome)
                body = b''
                if reason and request_proxy and transport_error:
                    from .web_proxy import reachable
                    if not reachable(request_proxy):
                        self.egress.fallback('proxy_unavailable', 'mid_run')
                        with self._guard:
                            if proxies := getattr(self.session, 'proxies', None):
                                proxies.clear()
                        reason = (PROXY_RETRY_DIRECT if response is None and self.policy.retry_direct and
                                  isinstance(exc, (requests.ConnectionError, requests.Timeout))
                                  else 'proxy_unavailable')
            finally:
                try:
                    if response is not None:
                        response.close()
                finally:
                    audit_reason = 'proxy_unavailable' if reason == PROXY_RETRY_DIRECT else reason
                    self._log_request(url, domain, kind, status, body, transfer_complete, audit_reason, audit_source)
            return response,body,reason,outcome

    def _log_request(self, url, domain, kind, status, body, complete, reason, audit_source):
        with self._guard, self.db:
            args = (self._audit_url(url, kind, preserve_encoding=True), domain, kst_now(self.clock).isoformat(),
                    status, kind, len(body))
            self.db.execute('''INSERT INTO request_log(url,domain,ts,status,kind,bytes,transfer_complete,run_id,adapter,outcome)
                               VALUES (?,?,?,?,?,?,?,?,?,?)''',
                            (*args, int(complete), self.run_id, audit_source,
                             reason or (f'http_{status}' if audit_source else None)))
            self.db.execute('INSERT INTO fetch_log(url,domain,ts,status,kind,bytes) VALUES (?,?,?,?,?,?)', args)
    def _walk_robots(self, target, *, url_allowed, audit_source, reservations=None):
        scope = registrable_domain(target)
        for hop in range(6):
            if reservations is not None and hostname(target) not in reservations:
                stopped, _ = self._stopped_reason(target)
                if stopped:
                    return None, b'', stopped
                with self._lock(target), self._guard:
                    attempt = self.host_policy.reserve_robots(target)
                if not attempt:
                    return None, b'', 'robots_pending'
                reservations[hostname(target)] = target, attempt
            response, raw, error, _ = self._request(target, 'robots', audit_source=audit_source)
            status = response.status_code if response is not None else None
            if error or status not in (301, 302, 303, 307, 308):
                return status, raw, error
            location = response.headers.get('Location')
            if hop == 5 or not location:
                break
            try:
                destination = urljoin(target, location)
                if ((self.public_scope and not _safe_public_query(destination))
                        or (url_allowed and not url_allowed(destination))):
                    break
                redirect = canonicalize(destination, preserve_path=True)
                if (registrable_domain(redirect) != scope or urlsplit(redirect).path != '/robots.txt'
                        or self._reason_denied(redirect) or not is_korean_news(redirect)
                        or same_fetch_target(redirect, target)):
                    break
            except (ValueError, UnicodeError):
                break
            target = redirect
        return status, b'', 'robots_redirect'

    def _news_robots_allowed(self, url, *, url_allowed, audit_source):
        host = hostname(url)
        with self._lock(url), self._guard:
            stopped, _ = self._stopped_reason(url)
            if stopped:
                return False, stopped
            body = self.host_policy.cached_robots(url)
            if body is None:
                attempt = self.host_policy.reserve_robots(url)
                if not attempt:
                    return False, 'robots_pending'
        if body is None:
            target = origin(url) + '/robots.txt'
            reservations = {host: (target, attempt)}
            status, raw, error = self._walk_robots(target, url_allowed=url_allowed, audit_source=audit_source,
                                                   reservations=reservations)
            decoded = raw.decode('utf-8', errors='replace')
            challenge = re.match(r'\s*(?:just a moment|captcha|verify you are human|access denied)', decoded, re.I)
            if decoded.lstrip().startswith('<'):
                if detect_outcome(raw, 'text/html', 200) in ('blocked', 'login_wall'):
                    challenge = True
                if status == 200:
                    error = error or 'robots_unavailable'
            if error or status not in (200, 404, 410) or challenge:
                for reserved_url, reserved_at in reservations.values():
                    with self._lock(reserved_url), self._guard:
                        self.host_policy.release_robots(reserved_url, reserved_at)
                        if challenge:
                            self.host_policy.refuse(reserved_url)
                            self._inaccessible(hostname(reserved_url), 'blocked')
                if status in BACKOFF_STATUSES:
                    return False, 'host_backoff'
                if challenge or status in BLOCK_STATUSES:
                    return False, 'blocked'
                if error in (PROXY_RETRY_DIRECT, 'proxy_unavailable', 'robots_pending', 'host_backoff',
                             'resource_hold', 'blocked', 'robots_redirect', 'cap_reached'):
                    return False, error
                return False, 'robots_unavailable'
            # Policy directives only. Percent escapes in paths are significant.
            directives = (line.split('#', 1)[0].strip() for line in decoded.splitlines()
                          if re.match(r'^\s*(user-agent|allow|disallow|crawl-delay)\s*:', line, re.I))
            body = '\n'.join(directives) if status == 200 else ''
            if strip_pii(unquote(body)) != unquote(body):
                body = 'User-agent: *\nDisallow: /'
            parser = KoreanNewsRobots(body, user_agent=USER_AGENT)
            for reserved_url, reserved_at in reservations.values():
                with self._lock(reserved_url), self._guard:
                    self.host_policy.store_robots(reserved_url, body, parser.delay, reserved_at)
        else:
            parser = KoreanNewsRobots(body, user_agent=USER_AGENT)
        self._delay[host] = parser.delay
        allowed = parser.can_fetch(url)
        return allowed, '' if allowed else 'robots_disallowed'

    def _robots_allowed(self, url, *, url_allowed=None, audit_source=None):
        if not is_korean_news(url):
            return True, ''
        # A second process must observe the completed cache write even if it
        # acquired the request lock just after the robots response closed.
        # The walk takes only one request lock at a time and never takes a
        # second policy lock when it follows a redirect to another host.
        with host_lock(self.settings.data_dir, hostname(url), robots=True):
            return self._news_robots_allowed(url, url_allowed=url_allowed, audit_source=audit_source)

    def fetch(self,url: str, *, kind: str = 'body', parent_url: str | None = None, task_id: str | None = None, discovered_via: str | None = None, _browser_shell: bool = False, api_headers: dict | None = None, subpage: bool = False, url_allowed=None, service_key: str | None = None, audit_source=None, preserve_path=False) -> FetchResult:
        """Guard each request and apply any caller-supplied URL scope before redirects."""
        if kind not in ('body','robots','sitemap','feed','api','browser_aux'):
            raise ValueError('Unsupported request kind')
        if service_key and (kind!='api' or urlsplit(url).netloc!='apis.data.go.kr'
                            or urlsplit(url).scheme!='https' or not self.public_scope):
            raise ValueError('invalid_license_request')
        ref=dict(parent_url=strip_pii(parent_url) if parent_url else None,task_id=task_id if task_id and self.db.execute('SELECT 1 FROM tasks WHERE task_id=?',(task_id,)).fetchone() else None,discovered_via=strip_pii(discovered_via) if discovered_via else None)
        preserve_path = preserve_path or subpage or self.policy.preserve_path
        request_identity = None
        def result(outcome,reason='',**kw):
            identity_url = ''
            if request_identity is not None:
                identity_url = url if url == request_identity else canonicalize(url, preserve_path=preserve_path)
                identity_url = self._audit_url(identity_url, kind)
            if outcome not in ('ok','denied','cap_reached'):
                safe_url = self._audit_url(url, kind)
                with self._guard,self.db:
                    self.db.execute("""INSERT INTO failures(failure_id,task_id,url,domain,reason,state,created_at)
                        VALUES (?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET state='pending'
                        WHERE failures.task_id IS NULL AND failures.reason='error' AND failures.state='resolved'""",
                        (deterministic_id(safe_url,ref['task_id'],outcome),ref['task_id'],safe_url,registrable_domain(url),outcome,'dead' if outcome in ('blocked','login_wall','robots_disallowed') else 'pending',kst_now(self.clock).isoformat()))
            elif outcome == 'ok' and kind == 'body' and ref['task_id'] is None:
                self._resolve_taskless_errors(request_identity, identity_url, url)
            effective_url = self._audit_url(url, kind, preserve_encoding=True)
            return FetchResult(effective_url,outcome,reason=reason,fetched_at=kst_now(self.clock).isoformat(),source_ref=ref,identity_url=identity_url,**kw)

        try:
            if self.public_scope and (not _safe_public_query(url) or
                                      self._unsafe_public_url(url, kind)):
                return FetchResult('', 'denied', reason='unsafe_url')
            if url_allowed and not url_allowed(url):
                return result('denied','outside requested URL scope')
            url=canonicalize(url,preserve_path=preserve_path)
        except (ValueError,UnicodeError):
            return result('denied','invalid URL')
        request_identity = url
        auth_origin=(urlsplit(url).scheme,urlsplit(url).netloc)
        initial_domain=registrable_domain(url)
        redirect_limit=2 if subpage else 5
        if api_headers and (kind!='api' or set(api_headers)!={'X-Subscription-Token'}):
            raise ValueError('Only scoped search API authentication is supported')
        https_probed=False
        retried_direct=False
        hop=0
        while hop <= redirect_limit:
            reason=self._reason_denied(url)
            if reason:
                return result('denied',reason)
            stopped, retry = self._stopped_reason(url)
            if stopped:
                return result('blocked', stopped, retry_after=retry)
            allowed,reason=(self._robots_allowed(url,url_allowed=url_allowed,audit_source=audit_source)
                            if kind != 'robots' else (True,''))
            if not allowed and reason=='robots_unavailable' and urlsplit(url).scheme=='http' and not https_probed:
                https_probed=True
                try:
                    upgraded=canonicalize(urlsplit(url)._replace(scheme='https').geturl(),preserve_path=True)
                except (ValueError,UnicodeError):
                    return result('denied','invalid URL')
                denied=self._reason_denied(upgraded)
                if denied:
                    return result('denied',denied)
                allowed,reason=self._robots_allowed(upgraded,url_allowed=url_allowed,audit_source=audit_source)
                url=upgraded
            if not allowed:
                if reason=='cap_reached':
                    return result('cap_reached',reason)
                if reason == PROXY_RETRY_DIRECT and not retried_direct:
                    retried_direct = True
                    continue
                if reason in ('robots_disallowed', 'robots_redirect'):
                    if reason == 'robots_disallowed':
                        self._inaccessible(hostname(url), reason)
                    return result('robots_disallowed', reason)
                stopped, retry = self._stopped_reason(url)
                return result('blocked' if stopped or reason in ('blocked', 'host_backoff', 'resource_hold') else 'error',
                              stopped or ('proxy_unavailable' if reason == PROXY_RETRY_DIRECT else reason),
                              retry_after=retry)
            response,body,error,outcome=self._request(url,kind,headers=api_headers if (urlsplit(url).scheme,urlsplit(url).netloc)==auth_origin else None, service_key=service_key, audit_source=audit_source)
            if error == PROXY_RETRY_DIRECT and not retried_direct:
                retried_direct=True
                continue
            if error:
                stopped, retry = self._stopped_reason(url)
                if stopped:
                    return result('blocked', stopped, retry_after=retry)
                return result('cap_reached' if error=='cap_reached' else 'error',
                              'proxy_unavailable' if error == PROXY_RETRY_DIRECT else error)
            status=response.status_code
            if service_key and 300<=status<400:
                return result('error','api_redirect',status=status)
            if 300<=status<400:
                if status not in (301,302,303,307,308):
                    return result('error','redirect_status',status=status)
                location=response.headers.get('Location')
                if not location:
                    return result('error','redirect limit',status=status)
                try:
                    destination=urljoin(url,location)
                    if ((self.public_scope and (not _safe_public_query(destination) or
                                                  self._unsafe_public_url(destination, kind)))
                            or (url_allowed and not url_allowed(destination))):
                        return result('denied','unsafe redirect',status=status)
                    # Keep the request path separate from its persistence identity.
                    redirect=canonicalize(destination,preserve_path=True)
                except (ValueError,UnicodeError):
                    return result('denied','invalid redirect',status=status)
                if subpage and (urlsplit(redirect).scheme!='https' or registrable_domain(redirect)!=initial_domain):
                    return result('denied','unsafe subpage redirect',status=status)
                if same_fetch_target(redirect,url):
                    return result('error','redirect_self_loop',status=status)
                if hop==redirect_limit:
                    return result('error','redirect limit',status=status)
                url=redirect
                hop+=1
                continue
            ct=response.headers.get('Content-Type','application/octet-stream').lower()
            if service_key:
                body=body.replace(service_key.encode(),b'[redacted]')
            if status in BACKOFF_STATUSES:
                return result('blocked', status=status, retry_after=self._stopped_reason(url)[1])
            if outcome is None:
                outcome=detect_outcome(body,ct,status,
                                       allow_short=self.policy.allow_short)
            # Excluded response bodies never leave this boundary or enter logs.
            if not self.public_scope and is_excluded(url,body.decode('utf-8',errors='replace')):
                return result('denied','excluded',status=status)
            preserve_shell=False
            if _browser_shell and outcome=='empty_body':
                from .extract.browser import needs_browser
                preserve_shell=needs_browser(body)
            links = None
            if self.discover_links and not subpage and kind == 'body' and outcome == 'ok' and 'html' in ct:
                links = [(anchor['href'], strip_pii(anchor.get_text(' ', strip=True)))
                         for anchor in BeautifulSoup(body, 'lxml').select('a[href]:not([download])')]
            sanitized=strip_pii(body) if not (service_key or 'pdf' in ct or ct.startswith('image/')) else body
            return result(outcome,status=status,content=sanitized if outcome=='ok' or preserve_shell else b'',content_type=ct,content_hash=content_hash(sanitized,ct) if outcome=='ok' else '',links=links)
        return result('error','redirect limit')
    def close(self):
        self.session.close()
        if self._host_policy is not None:
            self._host_policy.close()
