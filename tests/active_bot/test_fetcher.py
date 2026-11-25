import gzip
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote
import pytest
from hanset_active.crawl_policy import COLLECTION, DISCOVERY, LEGACY
from hanset_active.fetcher import Fetcher, USER_AGENT
from hanset_active.util.timeutil import FakeClock
from hanset_active.config import Settings
from hanset_active.db import connect, deterministic_id
from conftest import FIXTURES

@pytest.fixture
def venue():
    return (FIXTURES/'venue_en.html').read_bytes()

@pytest.fixture
def news_hosts(monkeypatch):
    monkeypatch.setattr('hanset_active.crawl_policy._NEWS_DOMAINS',
                        {'127.0.0.1', 'venue.example', 'example.com', 'example-sauna.test', 'other-sauna.test'})


def test_success_robots_and_spacing(bot,server,venue, news_hosts):
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

def test_robots_disallowed(bot,server,venue, news_hosts):
    server.add('/private',venue)
    assert bot.fetch(server.base+'/private').outcome=='robots_disallowed'
    assert [r['path'] for r in server.log]==['/robots.txt']
    assert bot.db.execute('SELECT state FROM sources').fetchone()[0]=='inaccessible'

@pytest.mark.parametrize('status,outcome',[(401,'blocked'),(403,'blocked'),(429,'blocked'),(302,'robots_disallowed')])
def test_robots_fail_closed(bot,server,status,outcome, news_hosts):
    server.add('/robots.txt','',status=status)
    assert bot.fetch(server.base+'/venue').outcome == outcome
    assert len(server.log)==1

@pytest.mark.parametrize('status',[404,410])
def test_robots_missing_allowed(bot,server,venue,status, news_hosts):
    server.add('/robots.txt','',status=status); server.add('/venue',venue)
    assert bot.fetch(server.base+'/venue').ok

def test_crawl_delay_and_persisted_cache(bot,server,venue, news_hosts):
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
    assert len(server.log)==1
    bot.settings.candidate_cap=1
    assert bot.register_candidate('https://venue-a.test')
    assert not bot.register_candidate('https://venue-a.test/')
    assert not bot.register_candidate('https://venue-b.test')
    assert bot.db.execute("SELECT used FROM budgets_daily WHERE kind='candidates'").fetchone()[0]==1
    bot.clock.sleep(86400)
    assert bot.register_candidate('https://venue-b.test')

def test_aux_cap(bot,server, news_hosts):
    bot.settings.aux_cap=0
    assert bot.fetch(server.base+'/venue').outcome=='cap_reached'
    assert not server.log

def test_redirect_per_hop(bot,server,venue,news_hosts):
    server.add('/start','',302,{'Location':'/venue'}); server.add('/venue',venue)
    assert bot.fetch(server.base+'/start').ok
    server.add('/denied','',302,{'Location':'https://instagram.com/'})
    assert bot.fetch(server.base+'/denied').outcome=='denied'
    server.add('/private-hop','',302,{'Location':'/private'})
    assert bot.fetch(server.base+'/private-hop').outcome=='robots_disallowed'
    assert '/private' not in [r['path'] for r in server.log]

def test_redirect_self_loop_immediate(bot, server):
    url = server.base + '/loop'
    server.add('/loop', '', 302, {'Location': url})
    result = bot.fetch(url)
    assert (result.outcome, result.reason, result.status) == ('error', 'redirect_self_loop', 302)
    assert [r['path'] for r in server.log] == ['/loop']
    assert bot.db.execute('SELECT url, reason, state FROM failures').fetchone()[:] == (url, 'error', 'pending')


def test_redirect_non_ascii_slash_preserved(bot, server, venue):
    wire_path = '/p/%EA%B0%80%EB%82%98-1'
    destination = wire_path.lower() + '/'
    server.add(wire_path, '', 301, {'Location': destination})
    server.add(wire_path + '/', venue)
    result = bot.fetch(server.base + '/p/가나-1')
    assert result.ok and result.url == server.base + destination
    assert result.identity_url == server.base + '/p/가나-1'
    assert [unquote(r['path']) for r in server.log] == ['/p/가나-1', '/p/가나-1/']
    for table in ('fetch_log', 'request_log'):
        row = bot.db.execute(f"SELECT url FROM {table} WHERE status=200 AND kind='body'").fetchone()
        assert row[0] == server.base + destination

@pytest.mark.parametrize(('body','expected'),[('<input type="password">sign in','login_wall'),('<main></main>','empty_body'),('<h1>Just a moment</h1>','blocked'),('<main>Page not found</main>','empty_body'),('<p>Cookie consent</p>','login_wall')])
def test_failure_outcomes(bot,server,body,expected):
    server.add('/failure',body)
    result=bot.fetch(server.base+'/failure')
    assert result.outcome==expected and not result.content
    if expected in ('blocked','login_wall'):
        before=len(server.log)
        assert bot.fetch(server.base+'/failure').reason == 'resource_hold'
        assert len(server.log)==before

@pytest.mark.parametrize('host',['instagram.com','www.instagram.com'])
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

@pytest.mark.parametrize('status',[429,503])
def test_retry_after_terminal(bot,server,status):
    server.add('/limited','Too many requests',status,{'Content-Type':'text/html','Retry-After':'120'})
    result=bot.fetch(server.base+'/limited')
    assert result.outcome=='blocked' and result.retry_after
    assert bot.fetch(server.base+'/limited').outcome=='blocked'
    assert len(server.log)==1
    assert bot.host_policy.stopped(server.base+'/other') == ('host_backoff', result.retry_after)

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
    assert bot.clock.monotonic()==0
    assert len(server.log)==4

def test_spacing_survives_fetcher_restart(bot,server,venue, news_hosts):
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

def test_unreachable_robots_fails_closed(bot,server,monkeypatch, news_hosts):
    import requests
    def unavailable(*args,**kwargs):
        raise requests.ConnectionError('offline fixture failure')
    monkeypatch.setattr(bot.session,'get',unavailable)
    result = bot.fetch(server.base+'/venue')
    assert (result.outcome, result.reason) == ('error', 'robots_unavailable')
    assert len(server.log)==0
    assert bot.db.execute('SELECT count(*) FROM request_log').fetchone()[0]==2

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
        route=routes[url]
        if isinstance(route,Exception):
            raise route
        status,headers,body=route
        return SimpleNamespace(status_code=status,headers=headers,iter_content=lambda _:iter([body]),close=lambda:None)
    monkeypatch.setattr(bot.session,'get',get)
    return routes,calls


@pytest.mark.parametrize('policy', [LEGACY, COLLECTION, DISCOVERY])
def test_root_redirect_adds_trailing_slash_once(bot, fake_http, venue, policy):
    routes, calls = fake_http
    bot.policy = policy
    bot.public_scope = policy is COLLECTION
    root = 'https://venue.example'
    start, destination = root + '/kr?x=1', root + '/kr/?x=1'
    routes[start] = (301, {'Location': '/kr/?x=1'}, b'')
    routes[destination] = (200, {'Content-Type': 'text/html'}, venue)

    result = bot.fetch(start)

    assert result.ok and result.url == destination
    assert result.identity_url == (destination if policy.preserve_path else start)
    assert [url for url, _ in calls] == list(routes)
    statuses = bot.db.execute("SELECT status FROM fetch_log WHERE kind='body' ORDER BY id")
    assert [row[0] for row in statuses] == [301, 200]


def test_redirect_recovery_clears_taskless_error(bot, fake_http, venue):
    routes, calls = fake_http
    bot.policy = COLLECTION
    bot.public_scope = True
    root = 'https://venue.example'
    start = root + '/p/가나-1'
    encoded = '%ea%b0%80%eb%82%98-1/'
    destination = root + '/other/' + encoded
    final_identity = root + '/other/가나-1'
    unrelated = root + '/other'
    routes[start] = (301, {'Location': destination}, b'')
    routes[destination] = (301, {'Location': '/other/' + encoded.upper()}, b'')
    assert bot.fetch(start).reason == 'redirect_self_loop'
    with bot.db:
        bot.db.executemany("INSERT INTO failures(failure_id, url, reason, state) VALUES (?, ?, 'error', 'pending')",
                           ((deterministic_id(url, None, 'error'), url) for url in (start, final_identity, unrelated)))

    routes[destination] = (200, {'Content-Type': 'text/html'}, venue)
    assert bot.fetch(start).ok
    states = dict(bot.db.execute('SELECT url, state FROM failures'))
    assert states == {start: 'resolved', final_identity: 'resolved', destination: 'resolved', unrelated: 'pending'}
    assert [url for url, _ in calls] == [start, destination] * 2

    routes[destination] = (500, {}, b'')
    assert bot.fetch(start).outcome == 'error'
    assert bot.db.execute('SELECT state FROM failures WHERE url=?', (destination,)).fetchone()[0] == 'pending'


def test_redirect_recovery_leaves_taskowned_failure(bot, fake_http, venue):
    routes, _ = fake_http
    root = 'https://venue.example'
    start = root + '/kr'
    routes[start] = (301, {'Location': '/kr/'}, b'')
    routes[root + '/kr/'] = (200, {'Content-Type': 'text/html'}, venue)
    with bot.db:
        bot.db.execute("INSERT INTO failures(failure_id, task_id, url, reason, state) VALUES (?, 't1', ?, 'error', 'pending')",
                       (deterministic_id(start, 't1', 'error'), start))
        bot.db.execute("INSERT INTO failures(failure_id, url, reason, state) VALUES (?, ?, 'error', 'dead')",
                       (deterministic_id(start, None, 'error'), start))

    assert bot.fetch(start).ok
    assert dict(bot.db.execute('SELECT task_id, state FROM failures')) == {'t1': 'pending', None: 'dead'}


@pytest.mark.parametrize(('path', 'destination'), [('/p/%EB%B3%B4', '/p/%eb%b3%b4'), ('', '/')])
def test_redirect_equivalent_target_is_self_loop(bot, fake_http, path, destination):
    routes, calls = fake_http
    root = 'https://venue.example'
    routes[root + path] = (302, {'Location': destination}, b'')

    result = bot.fetch(root + path, preserve_path=True)

    assert (result.outcome, result.reason) == ('error', 'redirect_self_loop')
    assert [url for url, _ in calls] == list(routes)


def test_redirect_query_only_is_progress(bot, fake_http, venue):
    routes, calls = fake_http
    root = 'https://venue.example'
    routes[root + '/a?x=1'] = (302, {'Location': '?x=2'}, b'')
    routes[root + '/a?x=2'] = (200, {'Content-Type': 'text/html'}, venue)

    assert bot.fetch(root + '/a?x=1').ok
    assert [url for url, _ in calls] == list(routes)


@pytest.mark.parametrize(('location', 'reason'), [('/contact/fixture%40mail.example/', 'invalid redirect'),
                                                  ('/%65xcluded-fixture/', 'excluded')])
def test_redirect_checks_decoded_path_before_request(bot, fake_http, location, reason, monkeypatch):
    from hanset_active.util.urls import is_excluded
    monkeypatch.setattr('hanset_active.fetcher.is_excluded',
                        lambda *values: is_excluded(*values, terms=('excluded-fixture',)))
    routes, calls = fake_http
    root = 'https://venue.example'
    routes[root + '/guide'] = (302, {'Location': location}, b'')

    result = bot.fetch(root + '/guide')

    assert (result.outcome, result.reason) == ('denied', reason)
    assert [url for url, _ in calls] == list(routes)


@pytest.mark.parametrize('escape', ['%EB%B3%B4', '%eb%b3%b4'])
def test_news_robots_deny_equivalent_redirect_escape(bot, fake_http, escape, monkeypatch):
    routes, calls = fake_http
    root = 'https://news.example'
    monkeypatch.setattr('hanset_active.crawl_policy._NEWS_DOMAINS', frozenset({'news.example'}))
    bot.policy = COLLECTION
    bot.public_scope = True
    routes[root + '/robots.txt'] = (200, {}, f'User-agent: *\nDisallow: /p/{escape}/$'.encode())
    routes[root + '/guide'] = (302, {'Location': '/p/%eb%b3%b4/'}, b'')

    result = bot.fetch(root + '/guide')

    assert result.outcome == 'robots_disallowed'
    assert [url for url, _ in calls] == list(routes)
    assert bot.db.execute('SELECT url FROM failures').fetchone()[0] == root + '/p/%eb%b3%b4/'


def test_root_redirect_chain_still_hits_limit(bot, fake_http):
    routes, calls = fake_http
    root = 'https://venue.example'
    for hop in range(6):
        routes[root + f'/g{hop}'] = (301, {'Location': f'/g{hop + 1}'}, b'')

    result = bot.fetch(root + '/g0')

    assert (result.outcome, result.reason) == ('error', 'redirect limit')
    assert [url for url, _ in calls] == list(routes)


def test_202_challenge_stays_blocked(bot, fake_http):
    routes, calls = fake_http
    bot.policy = COLLECTION
    bot.public_scope = True
    root = 'https://venue.example'
    routes[root + '/guide'] = (302, {'Location': '/guide/'}, b'')
    routes[root + '/guide/'] = (202, {'Content-Type': 'text/html'}, b'<h1>Verify you are human</h1>')

    result = bot.fetch(root + '/guide')

    assert result.outcome == 'blocked' and result.status == 202
    assert bot.fetch(root + '/guide/', preserve_path=True).reason == 'resource_hold'
    assert [url for url, _ in calls] == list(routes)


@pytest.mark.parametrize('path', ['/facilities/', '/guide%2Fsauna/', '/%EC%82%AC%EC%9A%B0%EB%82%98/'])
def test_subpage_requests_slash_form_directly(bot, fake_http, venue, path):
    routes, calls = fake_http
    root = 'https://venue.example'
    routes[root + path.rstrip('/')] = (307, {'Location': path}, b'')
    routes[root + path] = (200, {'Content-Type': 'text/html'}, venue)
    result = bot.fetch(root + path + '?utm_source=nav#info', subpage=True)
    assert result.ok
    assert [url for url, _ in calls] == [root + path]
    assert all(not kwargs['allow_redirects'] for _, kwargs in calls)


def test_subpage_redirect_preserves_path_and_checks_destination_robots(bot, fake_http, venue, news_hosts):
    routes, calls = fake_http
    root, destination = 'https://venue.example', 'https://info.venue.example'
    routes[root + '/robots.txt'] = (200, {}, b'User-agent: *\nAllow: /')
    routes[root + '/guide/'] = (307, {'Location': destination + '/sauna/?utm_source=nav#info'}, b'')
    routes[destination + '/robots.txt'] = (200, {}, b'User-agent: *\nAllow: /')
    routes[destination + '/sauna/'] = (200, {'Content-Type': 'text/html'}, venue)
    result = bot.fetch(root + '/guide/', subpage=True)
    assert result.ok and result.url == destination + '/sauna/'
    assert [url for url, _ in calls] == list(routes)
    assert bot.db.execute("SELECT used FROM budgets_daily WHERE kind='bodies'").fetchone()[0] == 2
    assert bot.db.execute("SELECT used FROM budgets_daily WHERE kind='aux'").fetchone()[0] == 2


def test_subpage_redirect_robots_denies_only_slash_path(bot, fake_http, news_hosts):
    routes, calls = fake_http
    root = 'https://venue.example'
    routes[root + '/robots.txt'] = (200, {}, b'User-agent: *\nDisallow: /sauna/\n')
    routes[root + '/guide/'] = (307, {'Location': '/sauna/'}, b'')
    result = bot.fetch(root + '/guide/', subpage=True)
    assert result.outcome == 'robots_disallowed' and result.url == root + '/sauna/'
    assert [url for url, _ in calls] == list(routes)


@pytest.mark.parametrize('destination', ['https://other.example/sauna/', 'http://venue.example/sauna/'])
def test_subpage_rejects_external_redirect_or_downgrade_before_any_destination_request(bot, fake_http, destination):
    routes, calls = fake_http
    root = 'https://venue.example'
    routes[root + '/guide/'] = (302, {'Location': destination}, b'')
    assert bot.fetch(root + '/guide/', subpage=True).outcome == 'denied'
    assert [url for url, _ in calls] == list(routes)


@pytest.mark.parametrize('hops', [2, 3])
def test_subpage_redirect_limit_is_two(bot, fake_http, venue, hops):
    routes, calls = fake_http
    root = 'https://venue.example'
    for index in range(hops):
        routes[root + f'/guide{index}/'] = (308, {'Location': f'/guide{index + 1}/'}, b'')
    routes[root + f'/guide{hops}/'] = (200, {'Content-Type': 'text/html'}, venue)
    result = bot.fetch(root + '/guide0/', subpage=True)
    assert result.outcome == ('ok' if hops == 2 else 'error')
    assert [url for url, _ in calls] == [root + f'/guide{i}/' for i in range(3)]


def test_default_fetch_retains_path_and_external_redirect_semantics(bot, fake_http, venue):
    routes, calls = fake_http
    root, destination = 'https://venue.example', 'https://other.example'
    routes[root + '/guide'] = (302, {'Location': destination + '/sauna/'}, b'')
    routes[destination + '/sauna/'] = (200, {'Content-Type': 'text/html'}, venue)
    assert bot.fetch(root + '/guide/').ok
    assert [url for url, _ in calls] == list(routes)


@pytest.mark.parametrize('destination', ['https://instagram.com/guide/', 'https://venue.example/runport/'])
def test_subpage_redirect_keeps_denials(bot, fake_http, destination):
    routes, calls = fake_http
    root = 'https://venue.example'
    routes[root + '/guide/'] = (302, {'Location': destination}, b'')
    assert bot.fetch(root + '/guide/', subpage=True).outcome == 'denied'
    assert [url for url, _ in calls] == list(routes)

@pytest.mark.parametrize('http_status',[None,500])
@pytest.mark.parametrize('https_status',[200,404])
def test_http_robots_unavailable_upgrades_to_https(bot,fake_http,venue,http_status,https_status, news_hosts):
    import requests
    routes,calls=fake_http
    origin='http://www.example-sauna.test'
    secure='https://www.example-sauna.test'
    path='/visit?day=sunday'
    routes[origin+'/robots.txt']=(http_status,{},b'') if http_status else requests.ConnectionError('offline fixture failure')
    routes[secure+'/robots.txt']=(https_status,{'Content-Type':'text/plain'},b'User-agent: *\nAllow: /' if https_status==200 else b'')
    routes[secure+path]=(200,{'Content-Type':'text/html'},venue)
    bot.settings.min_interval=5
    result=bot.fetch(origin+path)
    assert result.ok and result.url==secure+path
    assert [c[0] for c in calls]==list(routes)
    assert bot.clock.elapsed==10
    assert not bot._stopped
    assert not bot.db.execute("SELECT 1 FROM sources WHERE state='inaccessible'").fetchone()
    assert not bot.db.execute('SELECT 1 FROM failures').fetchone()
    assert bot.db.execute("SELECT used FROM budgets_daily WHERE kind='aux'").fetchone()[0]==2
    assert bot.db.execute("SELECT used FROM budgets_daily WHERE kind='bodies'").fetchone()[0]==1
    assert bot.session.headers['User-Agent']==USER_AGENT

@pytest.mark.parametrize('status,body',[(401,b''),(403,b''),(429,b''),(500,b''),
    (200,b'User-agent: *\nDisallow: /'),(200,b'<h1>Captcha</h1>')])
def test_https_upgrade_robots_still_fail_closed(bot,fake_http,status,body, news_hosts):
    routes,calls=fake_http
    routes['http://example-sauna.test/robots.txt']=(500,{},b'')
    routes['https://example-sauna.test/robots.txt']=(status,{'Content-Type':'text/plain'},body)
    result=bot.fetch('http://example-sauna.test/visit')
    expected = 'robots_disallowed' if b'Disallow' in body else 'error' if status == 500 else 'blocked'
    assert result.outcome == expected
    assert result.url == 'https://example-sauna.test/visit'
    assert [c[0] for c in calls] == list(routes)

@pytest.mark.parametrize('status,body',[(401,b''),(403,b''),(429,b''),
    (200,b'User-agent: *\nDisallow: /'),(200,b'<h1>Captcha</h1>')])
def test_http_robots_disallow_never_probes_https(bot,fake_http,status,body, news_hosts):
    routes,calls=fake_http
    routes['http://example-sauna.test/robots.txt']=(status,{'Content-Type':'text/plain'},body)
    expected = 'robots_disallowed' if b'Disallow' in body else 'blocked'
    assert bot.fetch('http://example-sauna.test/visit').outcome == expected
    assert [c[0] for c in calls] == list(routes)

def test_https_upgrade_respects_request_caps(bot,fake_http, news_hosts):
    routes,calls=fake_http
    bot.settings.aux_cap=1
    routes['http://example-sauna.test/robots.txt']=(500,{},b'')
    assert bot.fetch('http://example-sauna.test/visit').outcome=='cap_reached'
    assert [c[0] for c in calls]==list(routes)
    assert not bot._stopped
    assert not bot.db.execute('SELECT 1 FROM sources').fetchone()

def test_https_upgrade_respects_denylist(bot,fake_http,monkeypatch, news_hosts):
    routes,calls=fake_http
    routes['http://example-sauna.test/robots.txt']=(500,{},b'')
    original=bot._reason_denied
    monkeypatch.setattr(bot,'_reason_denied',lambda url:'denied host' if url.startswith('https:') else original(url))
    assert bot.fetch('http://example-sauna.test/visit').outcome=='denied'
    assert [c[0] for c in calls]==list(routes)

def test_https_upgrade_only_probes_once_after_redirect(bot,fake_http, news_hosts):
    routes,calls=fake_http
    routes['http://example-sauna.test/robots.txt']=(500,{},b'')
    routes['https://example-sauna.test/robots.txt']=(200,{},b'User-agent: *\nAllow: /')
    routes['https://example-sauna.test/visit']=(302,{'Location':'http://other-sauna.test/visit'},b'')
    routes['http://other-sauna.test/robots.txt']=(500,{},b'')
    assert bot.fetch('http://example-sauna.test/visit').reason=='robots_unavailable'
    assert [c[0] for c in calls]==list(routes)


def test_https_upgrade_preserves_redirect_slash(bot, fake_http, venue, news_hosts):
    routes, calls = fake_http
    insecure, secure = 'http://venue.example', 'https://venue.example'
    routes[secure + '/robots.txt'] = (200, {}, b'User-agent: *\nAllow: /')
    routes[secure + '/guide'] = (302, {'Location': insecure + '/visit/'}, b'')
    routes[insecure + '/robots.txt'] = (500, {}, b'')
    routes[secure + '/visit/'] = (200, {'Content-Type': 'text/html'}, venue)

    result = bot.fetch(secure + '/guide')

    assert result.ok and result.url == secure + '/visit/' and result.identity_url == secure + '/visit'
    assert [url for url, _ in calls] == list(routes)

def test_robots_same_site_redirect_budget_spacing(bot,fake_http,venue, news_hosts):
    routes,calls=fake_http
    bot.settings.min_interval=5
    routes['http://example.com/robots.txt']=(301,{'Location':'https://www.example.com/robots.txt'},b'')
    routes['https://www.example.com/robots.txt']=(200,{'Content-Type':'text/plain'},b'User-agent: *\nDisallow: /private')
    routes['http://example.com/page']=(200,{'Content-Type':'text/html'},venue)
    assert bot.fetch('http://example.com/page').ok
    assert [c[0] for c in calls]==list(routes)
    assert all(not c[1]['allow_redirects'] for c in calls)
    assert bot.session.headers['User-Agent']==USER_AGENT
    assert bot.clock.elapsed==5
    assert bot.db.execute("SELECT used FROM budgets_daily WHERE kind='aux'").fetchone()[0]==2
    assert bot.host_policy.cached_robots('http://example.com') == 'User-agent: *\nDisallow: /private'
    assert bot.fetch('http://example.com/private').outcome=='robots_disallowed'
    assert len(calls)==3

@pytest.mark.parametrize('location',['https://example.org/robots.txt','https://www.example.com/home',None])
def test_robots_redirect_rejected(bot,fake_http,location, news_hosts):
    routes,calls=fake_http
    routes['https://example.com/robots.txt']=(301,{'Location':location} if location else {},b'')
    assert bot.fetch('https://example.com/page').outcome=='robots_disallowed'
    assert len(calls)==1
    assert bot.host_policy.cached_robots('https://example.com') is None

@pytest.mark.parametrize('hops',[5,6])
def test_robots_redirect_hop_limit(bot,fake_http,venue,hops, news_hosts):
    routes,calls=fake_http
    urls=['https://example.com/robots.txt']+[f'https://hop{i}.example.com/robots.txt' for i in range(hops)]
    for start,end in zip(urls,urls[1:]): routes[start]=(302,{'Location':end},b'')
    routes[urls[-1]]=(200,{'Content-Type':'text/plain'},b'User-agent: *\nDisallow:')
    routes['https://example.com/page']=(200,{'Content-Type':'text/html'},venue)
    result=bot.fetch('https://example.com/page')
    assert result.outcome==('ok' if hops==5 else 'robots_disallowed')
    assert len(calls)==(7 if hops==5 else 6)

def test_robots_redirect_challenge_disallowed(bot,fake_http, news_hosts):
    routes,calls=fake_http
    routes['https://example.com/robots.txt']=(307,{'Location':'https://www.example.com/robots.txt'},b'')
    routes['https://www.example.com/robots.txt']=(200,{'Content-Type':'text/html'},b'<h1>Verify you are human</h1>')
    assert bot.fetch('https://example.com/page').outcome=='blocked'
    assert len(calls)==2

def test_robots_redirect_aux_budget_stops_hops(bot,fake_http, news_hosts):
    routes,calls=fake_http
    bot.settings.aux_cap=1
    routes['https://example.com/robots.txt']=(308,{'Location':'https://www.example.com/robots.txt'},b'')
    assert bot.fetch('https://example.com/page').outcome=='cap_reached'
    assert len(calls)==1
    assert not bot.db.execute('SELECT 1 FROM robots_cache').fetchone()
