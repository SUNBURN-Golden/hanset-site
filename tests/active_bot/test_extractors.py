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
    assert [l['kind'] for l in result['links']]==['excluded_link','booking','sitemap']
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
    assert [r['path'] for r in server.log]==['/robots.txt','/app']
    assert bot.db.execute("SELECT used FROM budgets_daily WHERE kind='bodies'").fetchone()[0]==1
