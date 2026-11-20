"""One identity, no bypasses: guarded HTTP with persistent budgets and robots."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import threading
from urllib.parse import urlsplit, urljoin
from urllib.robotparser import RobotFileParser
import re
import requests
from bs4 import BeautifulSoup
from .config import Settings
from .db import connect, consume_budget
from .util.timeutil import Clock, kst_now, kst_day
from .util.urls import canonicalize, registrable_domain, is_collect_denied, is_denied, is_excluded, literal_private
from .util.pii import strip_pii
from .util.hashing import content_hash

USER_AGENT='HansetActiveBot/1.0 (+mailto:hanset@soulbound.co.kr)'
BLOCK_STATUSES = (401, 403, 429, 202)

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
    @property
    def text(self):
        return self.content.decode('utf-8',errors='replace')
    @property
    def ok(self):
        return self.outcome=='ok'


def detect_outcome(content: bytes, content_type: str, status: int, *, public_scope=False) -> str:
    if status in BLOCK_STATUSES or (public_scope and status==402):
        return 'blocked'
    if status>=400:
        return 'error'
    if 'html' not in content_type and content.strip():
        return 'ok'
    soup=BeautifulSoup(content,'lxml')
    text=soup.get_text(' ',strip=True)
    if re.search(r'just a moment|verify you are human|captcha|access.denied|select all squares|anomaly|bot.challenge',text,re.I):
        return 'blocked'
    if soup.select_one('input[type="password"]') or (len(text)<500 and re.search(r'로그인|sign in|log in|cookie (?:wall|consent)',text,re.I)):
        return 'login_wall'
    if public_scope and re.search(r'\b(?:subscribe|subscription required|members only)\s+to (?:read|continue)|'
                                  r'\b(?:this (?:content|article) is for subscribers only|subscription required to access)\b|'
                                  r'구독자 전용|구독하시면.{0,24}(?:읽|볼 수)',text,re.I):
        return 'blocked'
    from .extract.html import extract
    main=extract(content)['text']
    if len(main)<80 or re.search(r'\b(?:page not found|404 not found)\b|페이지를 찾을 수 없',main,re.I):
        return 'empty_body'
    return 'ok'

class Fetcher:
    def __init__(self, settings: Settings | None = None, db=None, clock: Clock | None = None, proxy: str = '', *, public_scope=False):
        self.settings=settings or Settings()
        self.public_scope=public_scope
        # Collect-web can continue directly after a confirmed transport outage.
        self.proxy=''
        self.proxy_failed=False
        if proxy:
            from .web_proxy import configured
            self.proxy=configured({'HANSET_WEB_PROXY':proxy})
        self.db=db if db is not None else connect(self.settings.data_dir/'active.sqlite3')
        self.clock=clock or Clock()
        self.session=requests.Session()
        self.session.trust_env=False
        self.session.headers.update({'User-Agent':USER_AGENT,'Accept-Encoding':'gzip'})
        if self.proxy:
            self.session.proxies={'http':self.proxy,'https':self.proxy}
        self._locks={}
        self._guard=threading.RLock()
        self._global_lock=threading.Lock()
        self._last={}
        self._global_last=None
        self._delay={}
        self._robots={}
        self._stopped={}
    def _lock(self, domain):
        with self._guard:
            return self._locks.setdefault(domain,threading.RLock())
    def _reason_denied(self,url, *, registration=False):
        if self.public_scope and not registration:
            if is_collect_denied(url):
                return 'denied host'
        else:
            if is_denied(url,self.settings.profile['denied_domains']) or is_denied(url):
                return 'denied host'
            if is_excluded(url):
                return 'excluded'
        if literal_private(url):
            host=urlsplit(url).hostname
            if not (self.settings.testing and self.settings.allow_private_hosts and host in ('127.0.0.1','::1','localhost')):
                return 'private literal host'
        return ''
    def _wait(self,domain):
        now=self.clock.monotonic()
        if domain not in self._last:
            with self._guard:
                row=self.db.execute('SELECT ts FROM fetch_log WHERE domain=? ORDER BY id DESC LIMIT 1',(domain,)).fetchone()
            if row:
                age=(self.clock.now()-datetime.fromisoformat(row[0])).total_seconds()
                self._last[domain]=now-max(0,age)
        wait=max(0,self._last.get(domain, float('-inf'))+max(self.settings.min_interval,self._delay.get(domain,0))-now)
        self.clock.sleep(wait)
        with self._global_lock:
            if self._global_last is None:
                with self._guard:
                    row=self.db.execute('SELECT ts FROM fetch_log ORDER BY id DESC LIMIT 1').fetchone()
                if row:
                    self._global_last=self.clock.monotonic()-max(0,(self.clock.now()-datetime.fromisoformat(row[0])).total_seconds())
            if self._global_last is not None:
                self.clock.sleep(max(0,self._global_last+self.settings.global_gap-self.clock.monotonic()))
            self._global_last=self.clock.monotonic()
            self._last[domain]=self._global_last
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
                from .db import deterministic_id
                day=kst_day(self.clock)
                used=self.db.execute("SELECT used FROM budgets_daily WHERE day=? AND kind='candidates'",(day,)).fetchone()
                if (used[0] if used else 0)>=self.settings.candidate_cap:
                    return False
                self.db.execute("INSERT INTO budgets_daily VALUES (?,'candidates',1) ON CONFLICT(day,kind) DO UPDATE SET used=used+1",(day,))
                self.db.execute("INSERT INTO tasks(task_id,dedupe_key,url,domain,task_class,kind,priority,novelty,source_quality,created_at,updated_at) VALUES (?,?,?,?,'discover','trial',0.35,1,0.5,?,?)",(deterministic_id(url),url,url,registrable_domain(url),kst_now(self.clock).isoformat(),kst_now(self.clock).isoformat()))
                return True
    def _inaccessible(self,domain,reason):
        from .db import deterministic_id
        self._stopped[domain]=reason
        with self._guard, self.db:
            self.db.execute("INSERT INTO sources(source_id,domain,state,reason) VALUES (?,?,'inaccessible',?) ON CONFLICT(domain) DO UPDATE SET state='inaccessible',reason=excluded.reason",(deterministic_id(domain),domain,reason))
    def _request(self,url,kind,headers=None):
        domain=registrable_domain(url)
        with self._lock(domain):
            with self._guard:
                cap=self.settings.aux_cap if kind!='body' else self.settings.body_cap
                if not consume_budget(self.db,'aux' if kind!='body' else 'bodies',cap,self.clock):
                    return None,b'', 'cap_reached'
            self._wait(domain)
            status=None
            body=b''
            response=None
            reason=''
            proxy_down=False
            request_proxy=self.proxy
            try:
                response=self.session.get(url,headers=headers,allow_redirects=False,timeout=(self.settings.timeout,self.settings.timeout),stream=True)
                status=response.status_code
                ct=response.headers.get('Content-Type','').lower()
                limit=8*1024**2 if 'pdf' in ct else 4*1024**2 if ct.startswith('image/') else 2*1024**2
                from .web_proxy import is_heavy
                if self.public_scope and (status in BLOCK_STATUSES or status==402):
                    pass  # The response status already establishes the refusal.
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
            except (requests.RequestException, ValueError) as exc:
                reason=type(exc).__name__
                if request_proxy and isinstance(exc,requests.ConnectionError):
                    from .web_proxy import reachable
                    if not reachable(request_proxy):
                        proxy_down=True
            finally:
                if response is not None:
                    response.close()
                with self._guard, self.db:
                    args=(strip_pii(url),domain,kst_now(self.clock).isoformat(),status,kind,len(body))
                    self.db.execute('INSERT INTO request_log(url,domain,ts,status,kind,bytes) VALUES (?,?,?,?,?,?)',args)
                    self.db.execute('INSERT INTO fetch_log(url,domain,ts,status,kind,bytes) VALUES (?,?,?,?,?,?)',args)
            if proxy_down:
                with self._guard:
                    self.proxy_failed=True
                    if self.public_scope:
                        self.session.proxies.clear()
                        self.proxy=''
                if self.public_scope:
                    return None,b'','proxy_unavailable'
                from .web_proxy import ProxyUnavailable
                raise ProxyUnavailable('proxy_unavailable')
            return response,body,reason
    def _robots_allowed(self,url, *, mark_inaccessible=True, url_allowed=None):
        # fetch() defers the domain stop while considering one HTTPS origin.
        p=urlsplit(url)
        origin=p.scheme+'://'+p.netloc
        domain=registrable_domain(url)
        now=self.clock.now()
        with self._lock(domain):
            cached=self._robots.get(origin)
            if cached and cached[0]>now:
                parser,allowed,status=cached[1:]
            else:
                with self._guard:
                    row=self.db.execute('SELECT * FROM robots_cache WHERE origin=?',(origin,)).fetchone()
                if row and datetime.fromisoformat(row['expires_at'])>now:
                    body=row['body']; allowed=bool(row['allowed']); status=row['status']
                    expires=datetime.fromisoformat(row['expires_at'])
                else:
                    target=origin+'/robots.txt'
                    # RFC 9309: at most five robots redirects, restricted to
                    # this registrable domain and the /robots.txt path.
                    for hop in range(6):
                        response,raw,error=self._request(target,'robots')
                        status=response.status_code if response is not None else None
                        if error or status not in (301,302,303,307,308): break
                        location=response.headers.get('Location')
                        if hop==5 or not location: break
                        try:
                            destination=urljoin(target,location)
                            if url_allowed and not url_allowed(destination): break
                            redirect=canonicalize(destination)
                        except (ValueError,UnicodeError): break
                        if registrable_domain(redirect)!=domain or urlsplit(redirect).path!='/robots.txt' or self._reason_denied(redirect): break
                        target=redirect
                    if error in ('cap_reached','proxy_unavailable'):
                        return False,error
                    allowed=not error and (status==200 or (status is not None and 400<=status<500 and status not in (401,403,429)))
                    decoded=strip_pii(raw.decode('utf-8',errors='replace'))
                    body=decoded if status==200 else ''
                    if re.search('just a moment|captcha|verify you are human',decoded,re.I):
                        allowed=False
                    now=self.clock.now(); expires=now+timedelta(hours=24)
                    with self._guard,self.db:
                        self.db.execute('INSERT OR REPLACE INTO robots_cache VALUES (?,?,?,?,?,?,?)',(origin,body,status,now.isoformat(),expires.isoformat(),int(allowed),'' if allowed else 'robots unavailable'))
                parser=RobotFileParser()
                parser.parse(body.splitlines() if allowed else ['User-agent: *','Disallow: /'])
                delay=parser.crawl_delay(USER_AGENT) or parser.crawl_delay('*') or 0
                self._delay[domain]=max(self._delay.get(domain,0),delay)
                self._robots[origin]=(expires,parser,allowed,status)
            if not allowed or not parser.can_fetch(USER_AGENT,url):
                if mark_inaccessible:
                    self._inaccessible(domain,'robots_disallowed')
                elif status is None or 500<=status<600:
                    return False,'robots_unavailable'
                return False,'robots_disallowed'
            return True,''
    def fetch(self,url: str, *, kind: str = 'body', parent_url: str | None = None, task_id: str | None = None, discovered_via: str | None = None, _browser_shell: bool = False, api_headers: dict | None = None, subpage: bool = False, url_allowed=None) -> FetchResult:
        """Guard each request and apply any caller-supplied URL scope before redirects."""
        if kind not in ('body','robots','sitemap','feed','api','browser_aux'):
            raise ValueError('Unsupported request kind')
        ref=dict(parent_url=strip_pii(parent_url) if parent_url else None,task_id=task_id if task_id and self.db.execute('SELECT 1 FROM tasks WHERE task_id=?',(task_id,)).fetchone() else None,discovered_via=strip_pii(discovered_via) if discovered_via else None)
        def result(outcome,reason='',**kw):
            safe_url=strip_pii(url)
            if outcome not in ('ok','denied','cap_reached'):
                from .db import deterministic_id
                with self._guard,self.db:
                    self.db.execute('INSERT INTO failures(failure_id,task_id,url,domain,reason,state,created_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(failure_id) DO NOTHING',
                        (deterministic_id(safe_url,ref['task_id'],outcome),ref['task_id'],safe_url,registrable_domain(url),outcome,'dead' if outcome in ('blocked','login_wall','robots_disallowed') else 'pending',kst_now(self.clock).isoformat()))
            return FetchResult(safe_url,outcome,reason=reason,fetched_at=kst_now(self.clock).isoformat(),source_ref=ref,**kw)
        try:
            if url_allowed and not url_allowed(url):
                return result('denied','outside requested URL scope')
            url=canonicalize(url,preserve_path=subpage)
        except (ValueError,UnicodeError):
            return result('denied','invalid URL')
        auth_origin=(urlsplit(url).scheme,urlsplit(url).netloc)
        initial_domain=registrable_domain(url)
        redirect_limit=2 if subpage else 5
        if api_headers and (kind!='api' or set(api_headers)!={'X-Subscription-Token'}):
            raise ValueError('Only scoped search API authentication is supported')
        https_probed=False
        for hop in range(redirect_limit+1):
            reason=self._reason_denied(url)
            if reason:
                return result('denied',reason)
            domain=registrable_domain(url)
            with self._lock(domain):
                if domain in self._stopped:
                    return result('blocked',self._stopped[domain])
                allowed,reason=self._robots_allowed(url,mark_inaccessible=False,url_allowed=url_allowed)
                if not allowed and reason=='robots_unavailable' and urlsplit(url).scheme=='http' and not https_probed:
                    https_probed=True
                    upgraded=canonicalize(urlsplit(url)._replace(scheme='https').geturl(),preserve_path=subpage)
                    denied=self._reason_denied(upgraded)
                    if denied:
                        return result('denied',denied)
                    allowed,reason=self._robots_allowed(upgraded,mark_inaccessible=False)
                    if allowed:
                        url=upgraded
                if not allowed:
                    if reason=='cap_reached':
                        return result('cap_reached',reason)
                    if reason=='proxy_unavailable':
                        return result('error',reason)
                    self._inaccessible(domain,'robots_disallowed')
                    return result('robots_disallowed','robots_disallowed')
                response,body,error=self._request(url,kind,headers=api_headers if (urlsplit(url).scheme,urlsplit(url).netloc)==auth_origin else None)
                if error:
                    return result('cap_reached' if error=='cap_reached' else 'error',error)
                status=response.status_code
                if status in (301,302,303,307,308):
                    if hop==redirect_limit or not response.headers.get('Location'):
                        return result('error','redirect limit',status=status)
                    try:
                        destination=urljoin(url,response.headers['Location'])
                        if url_allowed and not url_allowed(destination):
                            return result('denied','unsafe redirect',status=status)
                        redirect=canonicalize(destination,preserve_path=subpage)
                    except (ValueError,UnicodeError):
                        return result('denied','invalid redirect')
                    if subpage and (urlsplit(redirect).scheme!='https' or registrable_domain(redirect)!=initial_domain):
                        return result('denied','unsafe subpage redirect',status=status)
                    url=redirect
                    continue
                ct=response.headers.get('Content-Type','application/octet-stream').lower()
                outcome=detect_outcome(body,ct,status,public_scope=self.public_scope)
                retry=None
                if status==429:
                    raw=response.headers.get('Retry-After','')
                    try:
                        retry=(self.clock.now()+timedelta(seconds=max(0,int(raw)))).isoformat()
                    except ValueError:
                        try:
                            retry=parsedate_to_datetime(raw).isoformat()
                        except (ValueError,TypeError):
                            retry=(self.clock.now()+timedelta(hours=24)).isoformat()
                if outcome in ('blocked','login_wall'):
                    self._inaccessible(domain,outcome)
                    if retry:
                        with self._guard,self.db:
                            self.db.execute('UPDATE sources SET next_at=? WHERE domain=?',(retry,domain))
                # Excluded response bodies never leave this boundary or enter logs.
                if not self.public_scope and is_excluded(url,body.decode('utf-8',errors='replace')):
                    return result('denied','excluded',status=status)
                preserve_shell=False
                if _browser_shell and outcome=='empty_body':
                    from .extract.browser import needs_browser
                    preserve_shell=needs_browser(body)
                sanitized=strip_pii(body) if not ('pdf' in ct or ct.startswith('image/')) else body
                return result(outcome,status=status,content=sanitized if outcome=='ok' or preserve_shell else b'',content_type=ct,content_hash=content_hash(sanitized,ct) if outcome=='ok' else '',retry_after=retry)
        return result('error','redirect limit')
    def close(self):
        self.session.close()
