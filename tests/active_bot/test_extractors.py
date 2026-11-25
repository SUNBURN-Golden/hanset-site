import json
import shutil
import sys
from pathlib import Path
import pytest
from hanset_active.extract import html, feed, sitemap, pdf, ocr, browser
from conftest import FIXTURES

@pytest.mark.parametrize('name',['venue_ko.html','venue_en.html'])
def test_html(name):
    result=html.extract((FIXTURES/name).read_bytes(),'https://venue-a.test')
    assert '25' in result['text'] and '90' in result['text']
    assert result['extractor']==html.NAME and result['extractor_version']==html.VERSION
    assert result['amounts']
    assert result['title']

def test_jsonld_tables_links_dates():
    result=html.extract((FIXTURES/'venue_ko.html').read_bytes(),'https://venue-a.test')
    assert result['structured']['jsonld'][0]['@type']=='LocalBusiness'
    assert len(result['structured']['tables'][0])==2
    assert result['dates']==['2026-10-20']
    assert result['mentions']
    assert {'internal','sns','attachment','image_notice'} <= {l['kind'] for l in result['links']}
    assert '하단 메뉴' not in result['text']
    assert html.extract((FIXTURES/'list.html').read_bytes(),'https://venue-a.test/list')['links'][-1]['kind']=='next_page'

def test_html_bad_json_and_link_types():
    result=html.extract('<script type="application/ld+json">bad</script><a href="https://booking.naver.com/x">booking</a><a href="https://booking.com/x">reserve</a><a href="/sitemap.xml">map</a>','https://venue-a.test')
    assert [l['kind'] for l in result['links']]==['external','booking','sitemap']
    assert not result['structured']['jsonld']

def test_feeds():
    assert feed.extract((FIXTURES/'rss.xml').read_bytes())['items'][0]['title']=='New hours'
    assert feed.extract((FIXTURES/'api.json').read_bytes())['items'][0]['price']==25000
    atom='<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Hours</title><link href="https://venue-a.test"/><summary>Open daily</summary></entry></feed>'
    assert feed.extract(atom)['items'][0]['url']=='https://venue-a.test'
    assert feed.extract('[{"title":"Hours"}]')['items'][0]['title']=='Hours'

def test_sitemaps():
    result=sitemap.extract((FIXTURES/'sitemap.xml').read_bytes())
    assert result['kind']=='index' and len(result['urls'])==1
    result=sitemap.extract('<urlset><url><loc>https://venue-a.test</loc><lastmod>2026-10-20</lastmod></url></urlset>')
    assert result['kind']=='urlset' and result['entries'][0]['lastmod']=='2026-10-20'

@pytest.mark.skipif(not shutil.which('pdftotext'),reason='pdftotext not installed')
def test_pdf(documents):
    result=pdf.extract(documents['pdf'])
    assert '25000' in result['text'] and '[page 1]' in result['text']
    assert result['pages'][0]['page']==1

@pytest.mark.skipif(not shutil.which('tesseract'),reason='tesseract not installed')
def test_ocr(documents):
    result=ocr.extract(documents['png'],languages='eng')
    if result['status']=='unavailable':
        pytest.skip('English language data not installed')
    assert '25000' in result['text']
    assert result['mean_confidence']>=60
    assert result['boxes']

def test_image_notice_heuristics(documents):
    assert ocr.is_image_notice(documents['png'],filename='price.png')
    assert not ocr.is_image_notice(documents['png'],filename='scenery.png')
    assert not ocr.is_image_notice(b'bad',filename='price.png')

def test_missing_tools(monkeypatch,documents):
    monkeypatch.setattr(shutil,'which',lambda _:None)
    assert pdf.extract(documents['pdf'])['status']=='unavailable'
    assert ocr.extract(documents['png'])['status']=='unavailable'

def test_browser_decision_and_lazy_import():
    assert browser.needs_browser('<div id="app"></div><script src="/app.js"></script>')
    assert not browser.needs_browser((FIXTURES/'venue_en.html').read_bytes())
    assert not browser.needs_browser('<div id="app">Just a moment</div><script src="x"></script>')
    assert not browser.needs_browser('<input type="password"><script src="x"></script>')
    assert not browser.needs_browser('<p>No application here</p>')
    assert 'playwright.sync_api' not in sys.modules
    assert browser.extract('<main>Example</main>')['extractor']=='browser'

def test_browser_denied_without_import(bot):
    assert browser.fetch(bot,'https://instagram.com/venue-a').outcome=='denied'
    assert 'playwright.sync_api' not in sys.modules

def test_extractor_versions():
    for module in (html,feed,sitemap,pdf,ocr,browser):
        assert module.NAME and module.VERSION

def test_browser_guarded_render(bot,server,monkeypatch):
    from types import SimpleNamespace
    server.add('/app','<div id="app"></div><script src="/app.js"></script>')
    rendered=(FIXTURES/'venue_en.html').read_text()
    class Route:
        request=SimpleNamespace(method='GET',url=server.base+'/app',is_navigation_request=lambda:True)
        def abort(self):
            pytest.fail('Permitted app shell was rejected')
        def fulfill(self,**kwargs):
            assert b'id="app"' in kwargs['body']
    class Page:
        def goto(self,*args,**kwargs):
            context.handler(Route())
        def content(self):
            return rendered
    class Context:
        def route(self,pattern,handler):
            self.handler=handler
        def route_web_socket(self,*args):
            pass
        def new_page(self):
            return Page()
    context=Context()
    class Browser:
        def new_context(self,**kwargs):
            assert kwargs['service_workers']=='block'
            return context
        def close(self):
            pass
    class Playwright:
        chromium=SimpleNamespace(launch=lambda **kwargs:Browser())
        def __enter__(self):
            return self
        def __exit__(self,*args):
            pass
    monkeypatch.setitem(sys.modules,'playwright.sync_api',SimpleNamespace(sync_playwright=Playwright))
    result=browser.fetch(bot,server.base+'/app')
    assert result.ok and result.fetch_method=='browser' and result.content_hash
    assert [r['path'] for r in server.log]==['/app']
    assert bot.db.execute("SELECT used FROM budgets_daily WHERE kind='bodies'").fetchone()[0]==1

def browser_request(url, *, method='GET', navigation=False, resource_type='document', abort_error=False):
    from types import SimpleNamespace
    return SimpleNamespace(method=method,url=url,resource_type=resource_type,abort_error=abort_error,is_navigation_request=lambda: navigation)

def install_fake_browser(monkeypatch, requests, rendered):
    from types import SimpleNamespace
    events=[]
    state={}
    class Route:
        def __init__(self, request):
            self.request=request
        def abort(self):
            events.append(('abort',self.request.url))
            if self.request.abort_error: raise RuntimeError('abort failed')
        def fulfill(self,**kwargs):
            events.append(('fulfill',self.request.url,kwargs.get('status'),kwargs.get('body')))
    class Page:
        def goto(self,*args,**kwargs):
            for request in requests: state['handler'](Route(request))
        def content(self):
            return rendered
    class Context:
        def route(self,pattern,handler):
            state['handler']=handler
        def route_web_socket(self,*args):
            events.append(('websocket',args[0] if args else ''))
        def new_page(self):
            return Page()
    class Browser:
        def new_context(self,**kwargs):
            events.append(('context',kwargs.get('service_workers')))
            return Context()
        def close(self):
            events.append(('close',))
    class Playwright:
        chromium=SimpleNamespace(launch=lambda **kwargs:Browser())
        def __enter__(self):
            return self
        def __exit__(self,*args):
            pass
    monkeypatch.setitem(sys.modules,'playwright.sync_api',SimpleNamespace(sync_playwright=Playwright))
    return events

SHELL='<div id="app"></div><script src="/app.js"></script>'

def test_browser_aux_failures_do_not_poison_document(bot,server,monkeypatch):
    from urllib.parse import urlsplit
    rendered=(FIXTURES/'venue_en.html').read_text()
    server.add('/app',SHELL)
    server.add('/style.css','nope',status=500,headers={'Content-Type':'text/css'})
    server.add('/empty.css','',status=200,headers={'Content-Type':'text/css'})
    server.add('/app.js','',status=404,headers={'Content-Type':'text/javascript'})
    server.add('/font.woff','',status=500,headers={'Content-Type':'font/woff'})
    server.add('/hero.png','',status=404,headers={'Content-Type':'image/png'})
    server.add('/pixel','',status=500,headers={'Content-Type':'application/octet-stream'})
    base=server.base
    requests=[
        browser_request(base+'/style.css',resource_type='stylesheet'),
        browser_request(base+'/empty.css',resource_type='stylesheet'),
        browser_request(base+'/app',navigation=True,resource_type='document'),
        browser_request(base+'/app.js',resource_type='script'),
        browser_request(base+'/font.woff',resource_type='font'),
        browser_request(base+'/hero.png',resource_type='image'),
        browser_request(base+'/pixel',resource_type='other'),
        browser_request(base+'/beacon',method='POST',resource_type='xhr'),
    ]
    events=install_fake_browser(monkeypatch,requests,rendered)
    result=browser.fetch(bot,base+'/app')
    assert result.ok and result.fetch_method=='browser' and b'Day pass' in result.content and b'25000' in result.content
    fulfilled=[event for event in events if event[0]=='fulfill']
    assert [event[1] for event in fulfilled]==[base+'/app']
    assert b'id="app"' in fulfilled[0][3]
    aborted={event[1] for event in events if event[0]=='abort'}
    assert {base+'/style.css',base+'/empty.css',base+'/app.js',base+'/font.woff',base+'/hero.png',base+'/pixel',base+'/beacon'}<=aborted
    assert ('websocket','**/*') in events and ('context','block') in events
    paths=[row['path'] for row in server.log]
    assert '/beacon' not in paths and {'/style.css','/empty.css','/app','/app.js','/font.woff','/hero.png','/pixel'}<=set(paths)
    kinds={urlsplit(row['url']).path:row['kind'] for row in bot.db.execute('SELECT kind,url FROM request_log')}
    assert kinds['/app']=='body' and kinds['/hero.png']=='body'
    assert kinds['/style.css']==kinds['/empty.css']==kinds['/app.js']==kinds['/font.woff']==kinds['/pixel']=='browser_aux'
    assert bot.db.execute("SELECT used FROM budgets_daily WHERE kind='bodies'").fetchone()[0]==2
    assert bot.db.execute("SELECT used FROM budgets_daily WHERE kind='aux'").fetchone()[0]==5

def test_browser_aux_abort_falls_back_to_empty_body(bot,server,monkeypatch):
    rendered=(FIXTURES/'venue_en.html').read_text()
    server.add('/app',SHELL)
    server.add('/style.css','',status=500,headers={'Content-Type':'text/css'})
    base=server.base
    requests=[
        browser_request(base+'/style.css',resource_type='stylesheet',abort_error=True),
        browser_request(base+'/app',navigation=True,resource_type='document'),
    ]
    events=install_fake_browser(monkeypatch,requests,rendered)
    result=browser.fetch(bot,base+'/app')
    assert result.ok and b'Day pass' in result.content
    empty=[event for event in events if event[0]=='fulfill' and event[1]==base+'/style.css']
    assert empty and empty[0][2]==200 and empty[0][3]==b''

def test_browser_document_error_discards_rendered_text(bot,server,monkeypatch):
    server.add('/app','<div id="app"></div>',status=500)
    events=install_fake_browser(monkeypatch,[browser_request(server.base+'/app',navigation=True)],(FIXTURES/'venue_en.html').read_text())
    result=browser.fetch(bot,server.base+'/app')
    assert result.outcome=='error' and result.content==b'' and result.fetch_method=='browser'
    assert any(event[0]=='abort' and event[1]==server.base+'/app' for event in events)
    assert '/app' in [row['path'] for row in server.log]

def test_browser_aux_error_does_not_hide_document_failure(bot,server,monkeypatch):
    server.add('/style.css','',status=500,headers={'Content-Type':'text/css'})
    server.add('/app','<input type="password"><p>sign in</p>')
    base=server.base
    requests=[
        browser_request(base+'/style.css',resource_type='stylesheet'),
        browser_request(base+'/app',navigation=True,resource_type='document'),
    ]
    install_fake_browser(monkeypatch,requests,(FIXTURES/'venue_en.html').read_text())
    result=browser.fetch(bot,base+'/app')
    assert result.outcome=='login_wall' and result.content==b''
    assert b'Day pass' not in result.content

def test_browser_blocked_aux_still_fails_render(bot,server,monkeypatch):
    server.add('/app',SHELL)
    server.add('/style.css','',status=403,headers={'Content-Type':'text/css'})
    base=server.base
    requests=[
        browser_request(base+'/app',navigation=True,resource_type='document'),
        browser_request(base+'/style.css',resource_type='stylesheet'),
    ]
    install_fake_browser(monkeypatch,requests,(FIXTURES/'venue_en.html').read_text())
    result=browser.fetch(bot,base+'/app')
    assert result.outcome=='blocked' and result.content==b''

def test_browser_nongget_document_is_denied(bot,server,monkeypatch):
    server.add('/app',SHELL)
    events=install_fake_browser(monkeypatch,[browser_request(server.base+'/app',method='POST',navigation=True)],(FIXTURES/'venue_en.html').read_text())
    result=browser.fetch(bot,server.base+'/app')
    assert result.outcome=='denied' and result.content==b''
    assert not any(event[0]=='fulfill' for event in events)
    assert '/app' not in [row['path'] for row in server.log]

def test_browser_denied_aux_still_fails_render(bot,server,monkeypatch):
    server.add('/app',SHELL)
    base=server.base
    requests=[
        browser_request(base+'/app',navigation=True,resource_type='document'),
        browser_request('https://instagram.com/widget.js',resource_type='script'),
    ]
    install_fake_browser(monkeypatch,requests,(FIXTURES/'venue_en.html').read_text())
    result=browser.fetch(bot,base+'/app')
    assert result.outcome=='denied' and result.content==b''
    assert all('instagram' not in row['path'] for row in server.log)

def test_browser_aux_cap_fails_render(bot,server,monkeypatch):
    from datetime import timedelta
    from hanset_active.util.timeutil import kst_now
    server.add('/app',SHELL)
    server.add('/style.css','',status=200,headers={'Content-Type':'text/css'})
    now=kst_now(bot.clock)
    with bot.db:
        bot.db.execute('INSERT INTO robots_cache VALUES (?,?,?,?,?,?,?)',(server.base,'User-agent: *\nAllow: /\n',200,now.isoformat(),(now+timedelta(days=1)).isoformat(),1,''))
    bot.settings.aux_cap=0
    requests=[
        browser_request(server.base+'/app',navigation=True,resource_type='document'),
        browser_request(server.base+'/style.css',resource_type='stylesheet'),
    ]
    install_fake_browser(monkeypatch,requests,(FIXTURES/'venue_en.html').read_text())
    result=browser.fetch(bot,server.base+'/app')
    assert result.outcome=='cap_reached' and result.content==b''
    assert '/style.css' not in [row['path'] for row in server.log]
    assert bot.db.execute("SELECT used FROM budgets_daily WHERE kind='bodies'").fetchone()[0]==1

def test_browser_requires_socket_guard(bot,server,monkeypatch):
    from types import SimpleNamespace
    server.add('/app',SHELL)
    class Context:
        def route(self,pattern,handler):
            pass
        def new_page(self):
            pytest.fail('socket guard must run before a page opens')
    class Browser:
        def new_context(self,**kwargs):
            return Context()
        def close(self):
            pass
    class Playwright:
        chromium=SimpleNamespace(launch=lambda **kwargs:Browser())
        def __enter__(self):
            return self
        def __exit__(self,*args):
            pass
    monkeypatch.setitem(sys.modules,'playwright.sync_api',SimpleNamespace(sync_playwright=Playwright))
    result=browser.fetch(bot,server.base+'/app')
    assert result.outcome=='error' and result.reason=='browser lacks socket guard'
