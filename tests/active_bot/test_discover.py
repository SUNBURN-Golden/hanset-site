import json
from urllib.parse import urlsplit, urlencode
import pytest
from requests.utils import requote_uri
from hanset_active.util.urls import canonicalize
from hanset_active.discover import Discovery
from hanset_active.discover.search import Nominatim, Wikipedia, Brave, DDGHtml, queries, discover as search_discover
from hanset_active.discover.directories import discover as directory_discover
from hanset_active.discover.sitemaps import expand, expand_feed, list_pages, robots_sitemaps
from hanset_active.discover.mentions import discover as mention_discover
from hanset_active.discover.links import discover as link_discover
from hanset_active.discover.reverse import followups
from hanset_active.venues import seed, upsert_venue

@pytest.mark.parametrize('provider_class,body',[
 (Nominatim,[{'class':'leisure','name':'Silver Sauna','extratags':{'website':'https://silver.example'},'display_name':'Silver Sauna Seoul'}]),
 (Wikipedia,{'query':{'search':[{'pageid':5,'title':'Silver Sauna','snippet':'Seoul Sauna'}]}}),
 (Brave,{'web':{'results':[{'url':'https://silver.example','title':'Silver Sauna'}]}}),
 (DDGHtml,'<div class="result"><a class="result__a" href="https://silver.example">Silver Sauna</a><span class="result__snippet">Seoul sauna welcome to this public directory listing of sauna facilities and visitor information.</span></div>')])
def test_providers(bot,server,provider_class,body,monkeypatch):
    context=Discovery(bot,budget=5)
    def getter(url,**kwargs): return context.get(url,**kwargs)
    getter.supports_brave_auth=True
    if provider_class is Brave: monkeypatch.setenv('BRAVE_API_KEY','fixture-only')
    provider=provider_class(getter,bot.db,clock=bot.clock,endpoint=server.base+'/search')
    route=urlsplit(canonicalize(server.base+'/search?'+urlencode(provider.params('sauna','en',10))))
    route=route.path+'?'+route.query
    server.add(route,json.dumps(body) if isinstance(body,(list,dict)) else body,headers={'Content-Type':'application/json' if provider_class is not DDGHtml else 'text/html'})
    hits=provider.search('sauna','en',10)
    assert hits and 'Sauna' in hits[0].title
    assert bot.db.execute('SELECT state FROM provider_state').fetchone()[0]=='ready'
    assert len(server.log)>=2

def test_brave_disabled(bot,monkeypatch):
    monkeypatch.delenv('BRAVE_API_KEY',raising=False)
    def fail(*args,**kwargs): raise AssertionError('network')
    assert Brave(fail,bot.db).search('sauna')==[]
    assert bot.db.execute('SELECT state FROM provider_state').fetchone()[0]=='disabled'

def test_ddg_challenge_no_alternative(bot,server):
    context=Discovery(bot)
    provider=DDGHtml(context.get,bot.db,clock=bot.clock,endpoint=server.base+'/search')
    server.add('/search?kl=kr-kr&q=sauna','Select all squares',status=202)
    assert provider.search('sauna')==[]
    count=len(server.log)
    assert provider.search('different')==[] and len(server.log)==count
    row=bot.db.execute('SELECT * FROM provider_state').fetchone()
    assert row['state']=='inaccessible' and row['next_at'][:10]>'2026-01-01'

def test_search_path(bot,server):
    seed(bot.db,[{'name':'Known Sauna','website':'https://known.example'}])
    context=Discovery(bot,budget=1)
    provider=Nominatim(context.get,bot.db,clock=bot.clock,endpoint=server.base+'/search')
    query,lang=next(queries(context.profile))
    server.add('/search?'+urlsplit(requote_uri(canonicalize(server.base+'/search?'+urlencode(provider.params(query,lang,10))))).query,json.dumps([{'class':'leisure','name':'Silver Sauna','display_name':'Seoul sauna','extratags':{'website':'https://silver.example'}}]),headers={'Content-Type':'application/json'})
    assert search_discover(context,[provider]), (server.log, [dict(r) for r in bot.db.execute("SELECT * FROM provider_state")])
    assert bot.db.execute('SELECT is_seed FROM venues').fetchone()[0]==0
    assert context.remaining==0

def test_sitemap_index_feed_robots_and_lists(bot,server):
    context=Discovery(bot,budget=15)
    server.add('/index.xml','<sitemapindex><sitemap><loc>'+server.base+'/map.xml</loc></sitemap></sitemapindex>',headers={'Content-Type':'application/xml'})
    server.add('/map.xml','<urlset><url><loc>https://silver.example/sauna</loc></url></urlset>',headers={'Content-Type':'application/xml'})
    assert expand(context,server.base+'/index.xml')
    server.add('/feed','<rss><channel><item><title>Seoul Sauna</title><link>https://gold.example</link><description>Sauna in Seoul</description></item></channel></rss>',headers={'Content-Type':'application/xml'})
    assert expand_feed(context,server.base+'/feed')
    server.add('/list','<main>'+('Official sauna directory ' * 8)+'<a href="https://blue.example">Blue Sauna Seoul</a><a rel="next" href="/list?page=2">Next</a></main>')
    server.add('/list?page=2','<main>'+('Sauna directory Seoul '*8)+'<a href="https://green.example">Green Sauna</a><a rel="next" href="/list">Next</a></main>')
    assert len(list_pages(context,server.base+'/list'))==2
    assert len([r for r in server.log if r['path'].startswith('/list')])==2
    assert directory_discover(context,{'kind':'html_list','url':server.base+'/list'})==[]
    assert directory_discover(context,{'kind':'rss','url':server.base+'/feed'})==[]
    assert directory_discover(context,{'kind':'sitemap','url':server.base+'/map.xml'})==[]
    assert directory_discover(context,{'enabled':False})==[]
    server.add('/robots.txt','User-agent: *\nSitemap: '+server.base+'/map.xml',headers={'Content-Type':'text/plain'})
    assert robots_sitemaps(context,server.base)==[] # Already registered.

def test_osm_directory(bot,server):
    context=Discovery(bot,budget=1)
    url=server.base+'/osm'
    provider=Nominatim(context.get,bot.db,endpoint=url)
    params=provider.params('sauna Seoul','en',10);params['offset']=0
    server.add('/osm?'+urlsplit(canonicalize(server.base+'/osm?'+urlencode(params))).query,json.dumps([{'class':'leisure','name':'Novel Sauna','extratags':{'website':'https://novel.example'}}]),headers={'Content-Type':'application/json'})
    assert directory_discover(context,{'kind':'osm_search','url':url,'paginate':True})
    assert bot.db.execute('SELECT discovered_via FROM venues').fetchone()[0]=='directory:osm_search'

def test_mentions_links_reverse_and_guards(bot):
    context=Discovery(bot,budget=4)
    vid=upsert_venue(bot.db,'Known Sauna',website='https://known.example')
    doc={'text':'Silver Sauna in Seoul','mentions':[{'name':'Silver Sauna'}]}
    assert mention_discover(context,doc,parent_url='https://known.example',region='Seoul')
    assert not mention_discover(context,doc,depth=2,region='Seoul')
    links=[{'url':'https://known.example/price','text':'요금','kind':'internal'}, {'url':'https://known.example/en/','text':'English','kind':'internal','hreflang':'en'}, {'url':'https://known.example/rates.pdf','text':'rates','kind':'attachment'}, {'url':'https://known.example/price.png','text':'price','kind':'image_notice'}, {'url':'https://instagram.com/silver','kind':'sns'}, {'url':'https://booking.naver.com/silver','kind':'excluded_link'}]
    assert len(link_discover(context,{'links':links},parent_url='https://known.example',venue_id=vid))==4
    assert bot.db.execute('SELECT count(*) FROM venue_links').fetchone()[0]==2
    assert not link_discover(context,{'links':links},parent_url='https://known.example',depth=2)
    venue=bot.db.execute('SELECT * FROM venues').fetchone()
    for strategy in ('site_links','lang_counterpart','attachments','images','search','directory'):
        assert followups(bot.db,venue,'price_day_pass',[{'links':links}],strategy=strategy)
    assert context.candidate('https://naver.com/test',low_prior=True) is None
    excluded=context.profile['exclusion_terms'][0]
    assert context.candidate('https://else.example',text=excluded,low_prior=True) is None
    assert not context.candidate('https://deep.example',low_prior=True,depth=3)
    assert max(r[0] for r in bot.db.execute('SELECT depth FROM tasks'))<=2

def test_budget_stop(bot,server):
    context=Discovery(bot,budget=0)
    assert context.get(server.base+'/anything') is None
    assert not server.log

def test_brave_auth_scoped_to_api_not_robots(bot,server,monkeypatch):
    monkeypatch.setenv('BRAVE_API_KEY','fixture-only')
    context=Discovery(bot)
    provider=Brave(context.get,bot.db,clock=bot.clock,endpoint=server.base+'/search')
    params=provider.params('sauna','en',10)
    route='/search?'+urlsplit(canonicalize(server.base+'/search?'+urlencode(params))).query
    server.add(route,json.dumps({'web':{'results':[]}}),headers={'Content-Type':'application/json'})
    assert provider.search('sauna','en')==[]
    api=next(row for row in server.log if row['path'].startswith('/search'))
    robots=next(row for row in server.log if row['path']=='/robots.txt')
    assert api['headers']['X-Subscription-Token']=='fixture-only'
    assert 'X-Subscription-Token' not in robots['headers']
    assert 'X-Subscription-Token' not in bot.session.headers

def test_bad_sitemap_and_limits(bot,server):
    context=Discovery(bot,budget=5)
    server.add('/bad.xml','broken XML',headers={'Content-Type':'application/xml'})
    assert expand(context,server.base+'/bad.xml')==[]
    assert expand(context,server.base+'/bad.xml',depth=3)==[]
    assert expand(context,server.base+'/bad.xml',max_candidates=0)==[]
