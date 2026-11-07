"""Lazy browser fallback; all requests remain behind Fetcher policy."""
import re
from bs4 import BeautifulSoup
from .html import extract as extract_html
from ..fetcher import FetchResult, detect_outcome, USER_AGENT
from ..util.pii import strip_pii
from ..util.hashing import content_hash
from ..util.urls import canonicalize, registrable_domain, is_excluded
from ..util.timeutil import kst_now
NAME='browser'
VERSION='1.0.0'
# Outcomes that must still discard the render. Transport failures of a
# stylesheet, script, font, image, or other subresource are not in this set.
_POLICY_OUTCOMES=frozenset({'denied','robots_disallowed','blocked','login_wall','cap_reached'})

def _document_request(request):
    return bool(request.is_navigation_request() or getattr(request,'resource_type','')=='document')

def _drop_request(route):
    """Abort one subrequest, or fulfill it empty when abort is unavailable."""
    try:
        route.abort()
        return
    except Exception:
        pass
    try:
        route.fulfill(status=200,body=b'',headers={'Content-Type':'text/plain'})
    except Exception:
        pass

def needs_browser(html: str | bytes) -> bool:
    if detect_outcome(html.encode() if isinstance(html,str) else html,'text/html',200) in ('blocked','login_wall'):
        return False
    soup=BeautifulSoup(html,'lxml')
    markers=soup.select_one('#app,#root,#__next,#__nuxt,[data-reactroot]')
    return len(extract_html(html)['text'])<200 and bool(markers or soup.select('script[src]'))

def fetch(fetcher, url: str) -> FetchResult:
    """Render a permitted app shell; subrequests use guarded HTTP, no socket bypass.

    Playwright is optional. Redirects, documents, and subresources go through the
    same limiter, robots check, and budget. A failed stylesheet, script, font,
    image, or other aux response is dropped for that request only. Navigation and
    document failures still abort the render, as do policy denials (a non-GET
    document, denylist, robots, block, login, or a budget cap). WebSocket
    connections are disabled. There is no socket bypass.
    """
    url=canonicalize(url)
    if fetcher._reason_denied(url):
        return FetchResult(strip_pii(url),'denied',reason='denied host',fetch_method=NAME)
    domain=registrable_domain(url)
    if domain in fetcher._stopped:
        return FetchResult(strip_pii(url),'blocked',fetch_method=NAME)
    allowed,reason=fetcher._robots_allowed(url)
    if not allowed:
        return FetchResult(strip_pii(url),reason,fetch_method=NAME)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return FetchResult(strip_pii(url),'error',reason='browser unavailable',fetch_method=NAME)
    failure=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            context=browser.new_context(user_agent=USER_AGENT,service_workers='block')
            def route_request(route):
                request=route.request
                document=_document_request(request)
                if request.method!='GET':
                    # A non-GET navigation is a policy denial. Aux beacons are
                    # refused too, but only the document request fails the render.
                    if document: failure.append('denied')
                    _drop_request(route); return
                # Images stay on the body budget. Other subresources stay aux.
                kind_body=document or getattr(request,'resource_type','')=='image'
                try:
                    r=fetcher.fetch(request.url,kind='body' if kind_body else 'browser_aux',_browser_shell=request.is_navigation_request())
                except Exception:
                    if document: failure.append('denied')
                    _drop_request(route); return
                shell=document and r.outcome=='empty_body' and r.content and needs_browser(r.content)
                if not r.ok and not shell:
                    if document or r.outcome in _POLICY_OUTCOMES: failure.append(r.outcome or 'error')
                    _drop_request(route); return
                try:
                    route.fulfill(status=r.status or 200,body=r.content,headers={'Content-Type':r.content_type})
                except Exception:
                    if document: failure.append('error')
                    else: _drop_request(route)
            context.route('**/*',route_request)
            # Newer Playwright supports explicit WebSocket routing; older versions
            # cannot provide the required network guarantee and are not used.
            if not hasattr(context,'route_web_socket'):
                return FetchResult(strip_pii(url),'error',reason='browser lacks socket guard',fetch_method=NAME)
            context.route_web_socket('**/*',lambda ws: ws.close())
            page=context.new_page()
            try:
                page.goto(url,wait_until='networkidle',timeout=15000)
                body=strip_pii(page.content()).encode()
            except Exception:
                return FetchResult(strip_pii(url),failure[0] if failure else 'error',reason='render failed',fetch_method=NAME)
            if failure:
                return FetchResult(strip_pii(url),failure[0],fetch_method=NAME)
            if is_excluded(body.decode()):
                return FetchResult(strip_pii(url),'denied',reason='excluded',fetch_method=NAME)
            outcome=detect_outcome(body,'text/html',200)
            return FetchResult(strip_pii(url),outcome,status=200,content=body if outcome=='ok' else b'',content_type='text/html',content_hash=content_hash(body,'text/html') if outcome=='ok' else '',fetch_method=NAME,fetched_at=kst_now(fetcher.clock).isoformat())
        finally:
            browser.close()

def extract(html: str | bytes, url: str = '') -> dict:
    result=extract_html(html,url)
    result.update(extractor=NAME,extractor_version=VERSION)
    return result
