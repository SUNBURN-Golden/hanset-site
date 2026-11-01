import gzip
from concurrent.futures import ThreadPoolExecutor
import pytest
from hanset_active.fetcher import Fetcher, USER_AGENT
from hanset_active.util.timeutil import FakeClock
from hanset_active.config import Settings
from hanset_active.db import connect
from conftest import FIXTURES

@pytest.fixture
def venue():
    return (FIXTURES/'venue_en.html').read_bytes()

def test_success_robots_and_spacing(bot,server,venue):
    bot.settings.min_interval=5; bot.settings.global_gap=1
    server.add('/venue',venue)
    assert bot.fetch(server.base+'/venue').ok
    assert bot.fetch(server.base+'/venue').ok
    assert [r['path'] for r in server.log]==['/robots.txt','/venue','/venue']
    assert all(r['headers']['User-Agent']==USER_AGENT for r in server.log)
    times=[r[0] for r in bot.db.execute('SELECT ts FROM request_log ORDER BY request_id')]
    from datetime import datetime
    assert all((datetime.fromisoformat(b)-datetime.fromisoformat(a)).total_seconds()>=5 for a,b in zip(times,times[1:]))
    assert bot.clock.monotonic()==10
    assert bot.db.execute("SELECT used FROM budgets_daily WHERE kind='bodies'").fetchone()[0]==2

def test_robots_disallowed(bot,server,venue):
    server.add('/private',venue)
    assert bot.fetch(server.base+'/private').outcome=='robots_disallowed'
    assert [r['path'] for r in server.log]==['/robots.txt']
    assert bot.db.execute('SELECT state FROM sources').fetchone()[0]=='inaccessible'

@pytest.mark.parametrize('status',[401,403,500,302])
def test_robots_fail_closed(bot,server,status):
    server.add('/robots.txt','',status=status)
    assert bot.fetch(server.base+'/venue').outcome=='robots_disallowed'
    assert len(server.log)==1

@pytest.mark.parametrize('status',[400,404,410])
def test_robots_missing_allowed(bot,server,venue,status):
    server.add('/robots.txt','',status=status); server.add('/venue',venue)
    assert bot.fetch(server.base+'/venue').ok

def test_crawl_delay_and_persisted_cache(bot,server,venue):
    server.add('/robots.txt','User-agent: *\nCrawl-delay: 8\nDisallow: /private\n',headers={'Content-Type':'text/plain'})
    server.add('/venue',venue)
    assert bot.fetch(server.base+'/venue').ok
    assert bot.clock.monotonic()==8
    second=Fetcher(bot.settings,bot.db,bot.clock)
    assert second.fetch(server.base+'/private').outcome=='robots_disallowed'
    assert len(server.log)==2
    second.close()

def test_caps(bot,server,venue):
    server.add('/venue',venue); bot.settings.body_cap=1
    assert bot.fetch(server.base+'/venue').ok
    assert bot.fetch(server.base+'/venue').outcome=='cap_reached'
    assert len(server.log)==2
    bot.settings.candidate_cap=1
    assert bot.register_candidate('https://venue-a.test')
    assert not bot.register_candidate('https://venue-a.test/')
    assert not bot.register_candidate('https://venue-b.test')
    assert bot.db.execute("SELECT used FROM budgets_daily WHERE kind='candidates'").fetchone()[0]==1
    bot.clock.sleep(86400)
    assert bot.register_candidate('https://venue-b.test')

def test_aux_cap(bot,server):
    bot.settings.aux_cap=0
    assert bot.fetch(server.base+'/venue').outcome=='cap_reached'
    assert not server.log

def test_redirect_per_hop(bot,server,venue):
    server.add('/start','',302,{'Location':'/venue'}); server.add('/venue',venue)
    assert bot.fetch(server.base+'/start').ok
    server.add('/denied','',302,{'Location':'https://naver.com/'})
    assert bot.fetch(server.base+'/denied').outcome=='denied'
    server.add('/private-hop','',302,{'Location':'/private'})
    assert bot.fetch(server.base+'/private-hop').outcome=='robots_disallowed'
    assert '/private' not in [r['path'] for r in server.log]

def test_redirect_limit(bot,server):
    server.add('/loop','',302,{'Location':'/loop'})
    assert bot.fetch(server.base+'/loop').outcome=='error'
    assert [r['path'] for r in server.log].count('/loop')==6

@pytest.mark.parametrize(('body','expected'),[('<input type="password">sign in','login_wall'),('<main></main>','empty_body'),('<h1>Just a moment</h1>','blocked'),('<main>Page not found</main>','empty_body'),('<p>Cookie consent</p>','login_wall')])
def test_failure_outcomes(bot,server,body,expected):
    server.add('/failure',body)
    result=bot.fetch(server.base+'/failure')
    assert result.outcome==expected and not result.content
    if expected in ('blocked','login_wall'):
        before=len(server.log)
        assert bot.fetch(server.base+'/another').outcome=='blocked'
        assert len(server.log)==before

@pytest.mark.parametrize('host',['naver.com','naver.me','booking.naver.com','pstatic.net','naver.net','a.naver.co.kr','instagram.com','www.instagram.com'])
def test_denied_before_network(bot,server,host):
    assert bot.fetch('https://'+host).outcome=='denied'
    assert not bot.register_candidate('https://'+host)
    assert not server.log

def test_private_and_proxy_literals(tmp_path):
    f=Fetcher(Settings(data_dir=tmp_path),connect(':memory:'),FakeClock())
    assert f.fetch('http://127.0.0.1').outcome=='denied'
    assert not f._reason_denied('http://198.18.0.1')
    assert not f._reason_denied('https://venue-a.test')
    f.close()

def test_size_limits_and_gzip(bot,server,venue):
    server.add('/large','x'*(2*1024**2+1))
    assert bot.fetch(server.base+'/large').reason=='size limit'
    server.add('/gzip',gzip.compress(venue),headers={'Content-Type':'text/html','Content-Encoding':'gzip'})
    assert bot.fetch(server.base+'/gzip').ok
    server.add('/gzip-large',gzip.compress(b'x'*(2*1024**2+1)),headers={'Content-Type':'text/html','Content-Encoding':'gzip'})
    assert bot.fetch(server.base+'/gzip-large').reason=='size limit'

def test_retry_after_terminal(bot,server):
    server.add('/limited','Too many requests',429,{'Content-Type':'text/html','Retry-After':'120'})
    result=bot.fetch(server.base+'/limited')
    assert result.outcome=='blocked' and result.retry_after
    assert bot.fetch(server.base+'/limited').outcome=='blocked'
    assert len(server.log)==2
    assert bot.db.execute('SELECT next_at FROM sources').fetchone()[0]==result.retry_after

def test_sanitized_and_excluded(bot,server,venue):
    email='fake'+'@'+'example.test'; secret='z'*40
    server.add('/pii',venue+b'<p>'+f'{email} token={secret}'.encode()+b'</p>')
    result=bot.fetch(server.base+'/pii',parent_url='https://venue-a.test/?token='+secret)
    assert email not in result.text and secret not in result.text
    assert secret not in result.source_ref['parent_url']
    term=bot.settings.profile['exclusion_terms'][0]
    server.add('/excluded',venue+term.encode())
    result=bot.fetch(server.base+'/excluded')
    assert result.outcome=='denied' and not result.content
    assert term not in str([tuple(r) for r in bot.db.execute('SELECT * FROM request_log')])

def test_concurrent_single_domain(bot,server,venue):
    bot.settings.min_interval=5
    server.add('/venue',venue)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:bot.fetch(server.base+'/venue'),range(4)))
    assert all(r.ok for r in results)
    assert bot.clock.monotonic()==20
    assert len(server.log)==5

def test_spacing_survives_fetcher_restart(bot,server,venue):
    bot.settings.min_interval=5
    server.add('/venue',venue)
    assert bot.fetch(server.base+'/venue').ok
    second=Fetcher(bot.settings,bot.db,bot.clock)
    assert second.fetch(server.base+'/venue').ok
    assert bot.clock.monotonic()==10
    second.close()

def test_browser_shell_is_failure_but_available_to_guarded_renderer(bot,server):
    server.add('/app','<div id="app"></div><script src="/app.js"></script>')
    ordinary=bot.fetch(server.base+'/app')
    assert ordinary.outcome=='empty_body' and not ordinary.content
    shell=bot.fetch(server.base+'/app',_browser_shell=True)
    assert shell.outcome=='empty_body' and shell.content
    assert bot.db.execute('SELECT reason FROM failures').fetchone()[0]=='empty_body'
    with pytest.raises(ValueError): bot.fetch(server.base+'/app',kind='uncounted')

def test_unreachable_robots_fails_closed(bot,server,monkeypatch):
    import requests
    def unavailable(*args,**kwargs):
        raise requests.ConnectionError('offline fixture failure')
    monkeypatch.setattr(bot.session,'get',unavailable)
    assert bot.fetch(server.base+'/venue').outcome=='robots_disallowed'
    assert len(server.log)==0
    assert bot.db.execute('SELECT count(*) FROM request_log').fetchone()[0]==1

def test_multi_page_fixture(bot,server):
    from hanset_active.extract.html import extract
    server.add('/list',(FIXTURES/'list.html').read_bytes())
    server.add('/list?page=2',(FIXTURES/'list_page2.html').read_bytes())
    first=bot.fetch(server.base+'/list')
    next_url=next(link['url'] for link in extract(first.content,first.url)['links'] if link['kind']=='next_page')
    second=bot.fetch(next_url)
    assert second.ok
    assert not any(link['kind']=='next_page' for link in extract(second.content,second.url)['links'])

@pytest.fixture
def fake_http(bot,monkeypatch):
    from types import SimpleNamespace
    routes={}; calls=[]
    def get(url,**kwargs):
        calls.append((url,kwargs))
        status,headers,body=routes[url]
        return SimpleNamespace(status_code=status,headers=headers,iter_content=lambda _:iter([body]),close=lambda:None)
    monkeypatch.setattr(bot.session,'get',get)
    return routes,calls

def test_robots_same_site_redirect_budget_spacing(bot,fake_http,venue):
    routes,calls=fake_http
    bot.settings.min_interval=5
    routes['http://example.com/robots.txt']=(301,{'Location':'https://www.example.com/robots.txt'},b'')
    routes['https://www.example.com/robots.txt']=(200,{'Content-Type':'text/plain'},b'User-agent: *\nDisallow: /private')
    routes['http://example.com/page']=(200,{'Content-Type':'text/html'},venue)
    assert bot.fetch('http://example.com/page').ok
    assert [c[0] for c in calls]==list(routes)
    assert all(not c[1]['allow_redirects'] for c in calls)
    assert bot.session.headers['User-Agent']==USER_AGENT
    assert bot.clock.elapsed==10
    assert bot.db.execute("SELECT used FROM budgets_daily WHERE kind='aux'").fetchone()[0]==2
    cached=bot.db.execute("SELECT * FROM robots_cache WHERE origin='http://example.com'").fetchone()
    assert cached['status']==200 and cached['allowed']==1
    assert bot.fetch('http://example.com/private').outcome=='robots_disallowed'
    assert len(calls)==3

@pytest.mark.parametrize('location',['https://example.org/robots.txt','https://www.example.com/home',None])
def test_robots_redirect_rejected(bot,fake_http,location):
    routes,calls=fake_http
    routes['https://example.com/robots.txt']=(301,{'Location':location} if location else {},b'')
    assert bot.fetch('https://example.com/page').outcome=='robots_disallowed'
    assert len(calls)==1
    assert bot.db.execute('SELECT status,allowed FROM robots_cache').fetchone()[:]==(301,0)

@pytest.mark.parametrize('hops',[5,6])
def test_robots_redirect_hop_limit(bot,fake_http,venue,hops):
    routes,calls=fake_http
    urls=['https://example.com/robots.txt']+[f'https://hop{i}.example.com/robots.txt' for i in range(hops)]
    for start,end in zip(urls,urls[1:]): routes[start]=(302,{'Location':end},b'')
    routes[urls[-1]]=(200,{'Content-Type':'text/plain'},b'User-agent: *\nDisallow:')
    routes['https://example.com/page']=(200,{'Content-Type':'text/html'},venue)
    result=bot.fetch('https://example.com/page')
    assert result.outcome==('ok' if hops==5 else 'robots_disallowed')
    assert len(calls)==(7 if hops==5 else 6)

def test_robots_redirect_challenge_disallowed(bot,fake_http):
    routes,calls=fake_http
    routes['https://example.com/robots.txt']=(307,{'Location':'https://www.example.com/robots.txt'},b'')
    routes['https://www.example.com/robots.txt']=(200,{'Content-Type':'text/html'},b'<h1>Verify you are human</h1>')
    assert bot.fetch('https://example.com/page').outcome=='robots_disallowed'
    assert len(calls)==2

def test_robots_redirect_aux_budget_stops_hops(bot,fake_http):
    routes,calls=fake_http
    bot.settings.aux_cap=1
    routes['https://example.com/robots.txt']=(308,{'Location':'https://www.example.com/robots.txt'},b'')
    assert bot.fetch('https://example.com/page').outcome=='cap_reached'
    assert len(calls)==1
    assert not bot.db.execute('SELECT 1 FROM robots_cache').fetchone()

def test_run_robots_expiry_retries_fresh_request(bot,fake_http,venue):
    from hanset_active.runner import run
    from hanset_active.util.timeutil import kst_now
    routes,calls=fake_http
    url='https://example.com/page'
    routes['https://example.com/robots.txt']=(503,{},b'')
    assert bot.register_candidate(url)
    with bot.db: bot.db.execute('INSERT INTO seed_domains VALUES (?,?,?,?)',('fixture','fixture','example.com',kst_now(bot.clock).isoformat()))
    run(bot,max_requests=1,initialize=False,providers=[])
    assert bot.db.execute('SELECT state FROM tasks').fetchone()[0]=='dead'
    assert run(bot,max_requests=2,initialize=False,providers=[])['requests']==0
    bot.clock.sleep(86401)
    routes['https://example.com/robots.txt']=(301,{'Location':'https://www.example.com/robots.txt'},b'')
    routes['https://www.example.com/robots.txt']=(200,{'Content-Type':'text/plain'},b'User-agent: *\nDisallow:')
    routes[url]=(200,{'Content-Type':'text/html'},venue)
    assert run(bot,max_requests=3,initialize=False,providers=[])['requests']==3
    assert bot.db.execute('SELECT state FROM tasks WHERE url=?',(url,)).fetchone()[0]=='done'
    assert bot.db.execute('SELECT state FROM sources').fetchone()[0]!='inaccessible'
    assert [c[0] for c in calls]==['https://example.com/robots.txt','https://example.com/robots.txt','https://www.example.com/robots.txt',url]
