"""Stage D discovery regressions, exclusively using the loopback fixture server."""
import json
from datetime import timedelta
from urllib.parse import quote
import pytest
from hanset_active.discover import Discovery
from hanset_active.discover.directories import discover, detail
from hanset_active.executor import Executor
from hanset_active.runner import bootstrap, recover, run
from hanset_active.scheduler import Scheduler, enqueue
from hanset_active.util.timeutil import kst_now
from hanset_active.venues import seed
from hanset_active.extract.fields import extract, extract_document


def blocked(bot):
    until=(kst_now(bot.clock)+timedelta(days=1)).isoformat()
    for name in ('nominatim','wikipedia','brave','ddg_html'):
        bot.db.execute('INSERT OR REPLACE INTO provider_state VALUES (?,?,?,?,?)',
                       (name,'inaccessible','fixture block',until,kst_now(bot.clock).isoformat()))
    bot.db.commit()
    return until


def test_slug_filter_before_cap_and_lastmod(bot,server):
    paths=['/unrelated/'+str(i) for i in range(600)]+['/sauna/blocked','/entertainment/'+quote('봉일스파랜드'),'/SPA/two','/spa/three']
    server.add('/index.xml',f'<sitemapindex><sitemap><loc>{server.base}/child.xml</loc></sitemap></sitemapindex>',headers={'Content-Type':'application/xml'})
    server.add('/child.xml','<urlset>'+''.join(f'<url><loc>{server.base}{p}</loc><lastmod>2026-01-01</lastmod></url>' for p in paths)+'</urlset>',headers={'Content-Type':'application/xml'})
    tasks=discover(Discovery(bot,budget=5),dict(id='fixture',kind='sitemap',url=server.base+'/index.xml',slug_include=['스파|sauna|spa'],slug_exclude=['blocked'],max_candidates=2))
    assert len(tasks)==2
    rows=bot.db.execute('SELECT * FROM tasks').fetchall()
    assert all(r['kind']=='trial' and r['urgency']>0 and r['priority']>.35 for r in rows)
    assert bot.db.execute("SELECT used FROM budgets_daily WHERE kind='candidates'").fetchone()[0]==2
    assert any('봉일' in r['url'] or '%EB' in r['url'] for r in rows)


def test_sitemap_directory_cap_not_domain_page_cap(bot,server):
    server.add('/map.xml','<urlset>'+''.join(f'<url><loc>{server.base}/spa/{i}</loc></url>' for i in range(80))+'</urlset>',headers={'Content-Type':'application/xml'})
    assert len(discover(Discovery(bot),dict(id='fixture',kind='sitemap',url=server.base+'/map.xml')))==60


def test_directory_detail_evidence_and_official_trial(bot,server):
    profile=bot.settings.profile
    profile['directories']=[dict(id='fixture',kind='sitemap',url=server.base+'/index.xml',directory=True)]
    bot.settings.profile_path=bot.settings.data_dir/'fixture-profile.json'
    bot.settings.profile_path.write_text(json.dumps(profile))
    # Different hostname identifies the official site without a real request.
    official='https://silver.example'
    body=f'''<html><head><meta property="og:title" content="Silver Sauna | Tourism Directory"></head>
    <main><h1>Silver Sauna</h1><p>Address: Seoul visitor district</p>
    <p>Day pass 25000 KRW. Opening hours 09:00 ~ 22:00.</p>
    <p>Public directory visitor information for Silver Sauna in Seoul.</p>
    <p>Website <a href="https://blog.naver.com/seed-spa-a">Review</a></p>
    <a href="https://other.example">Other</a><a href="{official}?utm_source=directory">Website</a>
    <a href="https://instagram.com/silverfixture">SNS</a><a href="https://booking.naver.com/fixture">Booking</a>
    <a href="https://booking.com/fixture">Reservations</a></main></html>'''
    parsed=detail(body,server.base+'/spa/silver',profile)
    assert parsed['website']==official and parsed['address']=='Seoul visitor district'
    seed(bot.db,[],clock=bot.clock)
    server.add('/spa/silver',body)
    ex=Executor(bot,providers=[],max_requests=10)
    ex.get(server.base+'/spa/silver')
    venue=bot.db.execute('SELECT * FROM venues').fetchone()
    assert venue['name']=='Silver Sauna' and venue['is_seed']==0
    assert venue['website']==official and venue['discovered_via']=='directory:fixture'
    assert json.loads(venue['domains'])==['silver.example']
    rows=bot.db.execute('SELECT * FROM observations').fetchall()
    assert {'name','website','address','price_day_pass','hours'}<={r['field'] for r in rows}
    assert all(r['source_type']=='official_directory' and r['label_status']=='fact' for r in rows)
    assert bot.db.execute("SELECT kind FROM tasks WHERE url=?",(official,)).fetchone()[0]=='trial'
    assert bot.db.execute("SELECT count(*) FROM venue_links WHERE kind='sns'").fetchone()[0]==1
    assert bot.db.execute("SELECT count(*) FROM venue_links WHERE kind='excluded_link'").fetchone()[0]==0
    assert bot.db.execute('SELECT count(*) FROM gaps').fetchone()[0]==9
    assert not bot.db.execute("SELECT 1 FROM tasks WHERE state='pending' AND kind IS NULL").fetchone()
    assert all('instagram' not in r['path'] for r in server.log)
    # A second listing is a different venue, never merged on the directory domain.
    server.add('/spa/gold',body.replace('Silver','Gold').replace('silver.example','gold.example'))
    ex.get(server.base+'/spa/gold')
    assert bot.db.execute('SELECT count(*) FROM venues').fetchone()[0]==2


def test_blocked_provider_defers_without_spending_share(bot):
    until=blocked(bot)
    search=enqueue(bot.db,kind='search',payload={'provider':'nominatim','query':'sauna'},novelty=10,clock=bot.clock)
    trial=enqueue(bot.db,kind='trial',url='https://silver.example',clock=bot.clock)
    sched=Scheduler(bot.db,clock=bot.clock,budget=1)
    assert sched.pick()['task_id']==trial
    row=bot.db.execute('SELECT * FROM tasks WHERE task_id=?',(search,)).fetchone()
    assert row['next_at']==until and row['attempts']==0 and row['state']=='pending'
    assert not bot.db.execute('SELECT 1 FROM failures').fetchone()


def test_search_generation_bounded_and_no_disabled_flood(bot):
    blocked(bot)
    bootstrap(bot)
    assert not bot.db.execute("SELECT 1 FROM tasks WHERE kind='search'").fetchone()
    bot.db.execute('DELETE FROM provider_state');bot.db.commit()
    bootstrap(bot)
    assert bot.db.execute("SELECT count(*) FROM tasks WHERE kind='search' AND state='pending'").fetchone()[0]==200
    assert enqueue(bot.db,kind='search',payload={'query':'extra'}) is None
    bootstrap(bot)
    assert bot.db.execute("SELECT count(*) FROM tasks WHERE kind='search'").fetchone()[0]==200


def test_legacy_null_task_recovery_and_dry_smoke(bot,server):
    bot.register_candidate(server.base+'/spa')
    assert bot.db.execute('SELECT kind FROM tasks').fetchone()[0]=='trial'
    bot.db.execute('UPDATE tasks SET kind=NULL');bot.db.commit()
    recover(bot.db,clock=bot.clock)
    assert bot.db.execute('SELECT kind FROM tasks').fetchone()[0]=='trial'
    assert run(bot,dry_run=True)['requests']==0
    assert server.log==[]


@pytest.mark.parametrize('text,field,value',[
 ('이용시간: 6시 ~ 22시','hours',{'hours':'06:00-22:00','scope':'unspecified'}),
 ('휴무: 매주 화요일','closures','휴무: 매주 화요일'),
 ('문신 안내: 문신 고객은 입장이 제한됩니다','tattoo_policy','not_allowed'),
 ('외국인 안내: 외국인 입장 가능합니다','foreigner_policy','accepted'),
 ('Sauna 외국인 고객은 여권 지참이 필요합니다','foreigner_policy','id_required'),
 ('일일 이용권 성인 요금 2만5천원','price_day_pass',{'amount':25000,'scope':'unspecified','ticket':'day_pass','audience':'adult'}),
 ('이용시간 평일 09:00 ~ 23:00','hours',{'hours':'09:00-23:00','scope':'weekday'}),
 ('Opening hours weekdays 9 am to 10 pm','hours',{'hours':'09:00-22:00','scope':'weekday'}),
 ('Closed every Tuesday','closures','Closed every Tuesday'),
 ('Tattoos are not permitted','tattoo_policy','not_allowed'),
 ('Sauna Foreign visitors are welcome','foreigner_policy','accepted'),
 ('Sauna Foreigners must bring a passport','foreigner_policy','id_required'),
 ('Single entry admission 25000 KRW','price_day_pass',{'amount':25000,'scope':'unspecified','ticket':'day_pass'}),
 ('Sauna English guide available','foreigner_policy','english_guide'),
])
def test_additional_bilingual_rules(text,field,value):
    assert any(r['field']==field and r['value']==value and r['quote'] for r in extract(text))


def test_korean_price_table_header_context():
    rows=extract_document({'structured':{'tables':[[['구분','요금'],['성인','25,000원'],['소인','15,000원']]]}})
    prices=[r for r in rows if r['field']=='price_day_pass']
    assert {r['value']['amount'] for r in prices}=={25000,15000}
    assert all(not r['ambiguous'] and 'selector' in r['locator'] for r in prices)


def test_low_prior_trials_bound_per_run(bot):
    for i in range(12):
        enqueue(bot.db,kind='trial',url=f'https://directory.example/id/{i}')
    sched=Scheduler(bot.db,clock=bot.clock,budget=20,trial_limits={'directory.example':5})
    for _ in range(5): assert sched.pick()
    assert sched.pick() is None
    assert sched.used['discover']==5
    # The next run can continue; these are not failures or dead letters.
    assert Scheduler(bot.db,clock=bot.clock,trial_limits={'directory.example':5}).pick()


def test_osm_and_reverse_search_also_deferred(bot):
    until=blocked(bot)
    for payload in ({'kind':'osm_search','url':'https://directory.example'}, {'query':'sauna'}):
        enqueue(bot.db,kind='directory',payload=payload,clock=bot.clock)
    sched=Scheduler(bot.db,clock=bot.clock)
    assert sched.pick() is None
    assert sum(sched.used.values())==0
    assert all(r[0]==until for r in bot.db.execute('SELECT next_at FROM tasks'))


def test_disabled_provider_direct_search_no_request(bot):
    from hanset_active.discover.search import Nominatim
    until=blocked(bot)
    bot.db.execute("UPDATE provider_state SET state='disabled' WHERE provider='nominatim'");bot.db.commit()
    def fail(*args,**kwargs): raise AssertionError('must not fetch')
    assert Nominatim(fail,bot.db,clock=bot.clock).search('sauna')==[]


def test_new_domain_evaluated_before_following_depth_two(bot,server):
    body='<title>Silver Sauna Seoul</title><main>'+('Silver Sauna Seoul visitor rules and pricing. '*10)+'<p>Day pass 25000 KRW</p><a href="/rules">Rules</a></main>'
    server.add('/official',body)
    ex=Executor(bot,providers=[],max_requests=10)
    tid=enqueue(bot.db,kind='trial',url=server.base+'/official',depth=1)
    ex.execute(dict(bot.db.execute('SELECT * FROM tasks WHERE task_id=?',(tid,)).fetchone()))
    assert bot.db.execute('SELECT 1 FROM source_trials').fetchone()
    assert bot.db.execute("SELECT state FROM sources").fetchone()[0]=='registered'
    assert bot.db.execute("SELECT depth FROM tasks WHERE url=?",(server.base+'/rules',)).fetchone()[0]==2


def test_specific_provider_uses_its_own_deferral_time(bot):
    until=blocked(bot)
    later=(kst_now(bot.clock)+timedelta(days=3)).isoformat()
    bot.db.execute("UPDATE provider_state SET next_at=? WHERE provider='nominatim'",(later,));bot.db.commit()
    tid=enqueue(bot.db,kind='search',payload={'query':'sauna','provider':'nominatim'})
    assert Scheduler(bot.db,clock=bot.clock).pick() is None
    assert bot.db.execute('SELECT next_at FROM tasks WHERE task_id=?',(tid,)).fetchone()[0]==later


def test_directory_bootstrap_revisits_completed_roots(bot):
    blocked(bot)
    bootstrap(bot)
    bot.db.execute("UPDATE tasks SET state='done' WHERE kind='directory'");bot.db.commit()
    bootstrap(bot)
    assert bot.db.execute("SELECT count(*) FROM tasks WHERE kind='directory' AND state='pending'").fetchone()[0]>=2


def test_offline_directory_to_official_fixture_pipeline(bot,server):
    official=server.base.replace('127.0.0.1','localhost')+'/official'
    profile=bot.settings.profile
    profile['directories']=[dict(id='fixture',kind='sitemap',url=server.base+'/index.xml',directory=True)]
    bot.settings.profile_path=bot.settings.data_dir/'profile.json'
    bot.settings.profile_path.write_text(json.dumps(profile))
    seed(bot.db,[{'name':'Silver Sauna','website':official}],clock=bot.clock)
    server.add('/spa/silver','<title>Silver Sauna | Tourism</title><main>'+('Public directory visitor information for Silver Sauna in Seoul. '*5)+f'<p>주소: 서울 방문자 지구</p><a href="{official}">홈페이지</a></main>')
    server.add('/official','<title>Silver Sauna Seoul</title><main>'+('Silver Sauna Seoul official visitor information. '*8)+'''<p>Day pass 25000 KRW</p>
    <a href="/price">Price</a><a hreflang="en" href="/en/rules">English rules</a>
    <a href="/rates.pdf">Price attachment</a><img src="/rates.png" alt="price timetable"></main>''')
    ex=Executor(bot,providers=[],max_requests=20)
    ex.get(server.base+'/spa/silver')
    task=dict(bot.db.execute('SELECT * FROM tasks WHERE url=?',(official,)).fetchone())
    assert task['kind']=='trial'
    ex.execute(task)
    venue=bot.db.execute('SELECT * FROM venues').fetchone()
    assert venue['is_seed']==1
    assert bot.db.execute("SELECT 1 FROM observations WHERE source_type='own_site' AND field='price_day_pass'").fetchone()
    strategies={r[0] for r in bot.db.execute('SELECT strategy FROM gap_attempts')}
    assert {'site_links','lang_counterpart','attachments','images'}<=strategies
    assert '/official' in [r['path'] for r in server.log]


def test_naver_blog_links_are_external_and_instagram_is_social():
    from hanset_active.extract.html import extract as html_extract
    links=html_extract('<a href="https://blog.naver.com/fixture">SNS</a><a href="https://instagr.am/fixture">SNS</a>')['links']
    assert [link['kind'] for link in links]==['external','sns']


def test_registered_directory_xml_does_not_bypass_slug_filter(bot,server):
    from hanset_active.db import deterministic_id
    domain='127.0.0.1'
    bot.db.execute("INSERT INTO sources(source_id,domain,state) VALUES (?,?,'registered')",(deterministic_id(domain),domain));bot.db.commit()
    server.add('/map.xml','<urlset>'+''.join(f'<url><loc>{server.base}/unrelated/{i}</loc></url>' for i in range(30))+f'<url><loc>{server.base}/spa/silver</loc></url></urlset>',headers={'Content-Type':'application/xml'})
    ex=Executor(bot,providers=[],max_requests=10)
    tid=enqueue(bot.db,kind='directory',payload={'id':'fixture','kind':'sitemap','url':server.base+'/map.xml','slug_include':['spa']})
    ex.execute(dict(bot.db.execute('SELECT * FROM tasks WHERE task_id=?',(tid,)).fetchone()))
    rows=bot.db.execute("SELECT url FROM tasks WHERE kind='trial'").fetchall()
    assert [r[0] for r in rows]==[server.base+'/spa/silver']
    assert bot.db.execute("SELECT used FROM budgets_daily WHERE kind='candidates'").fetchone()[0]==1


def test_malformed_href_and_none_pii_do_not_fail_page():
    from hanset_active.extract import html
    from hanset_active.util.pii import strip_pii
    doc = html.extract('<html><body><p>hello world sauna</p><a href="http://[bad/">x</a><a href="/ok">ok</a></body></html>', 'https://venue-a.test/')
    assert any(l['url'].endswith('/ok') for l in doc['links'])
    assert strip_pii(None) == ''


def test_long_url_paths_are_not_mistaken_for_tokens():
    from hanset_active.util.pii import strip_pii
    url = 'https://korean.visitseoul.net/sitemap/entertainment_kr_sitemap.xml'
    assert strip_pii(url) == url
    assert strip_pii('x ' + 'aB3' * 14) == 'x [token]'
    assert '[token]' in strip_pii('key=' + 'a1' * 20)


def test_venue_relevance_gate():
    from hanset_active.venues import venue_relevant
    assert not venue_relevant('놀부유황오리진흙구이(잠실점)', 'entertainment 유황 오리')
    assert not venue_relevant('Cafe Space Seoul', 'cafe')
    assert venue_relevant('봉일스파랜드', '')
    assert venue_relevant('우리유황온천', '온천')
    assert venue_relevant('CrossFit Hannam')
    assert venue_relevant('Dragon Hill', 'jjimjilbang bathhouse')
    assert not venue_relevant('Arario Museum in Space', 'museum')


def test_upsert_gate_and_seed_path(tmp_path):
    from hanset_active.db import connect, migrate
    from hanset_active.venues import upsert_venue
    db = connect(tmp_path / 'a.sqlite3'); migrate(db)
    assert upsert_venue(db, '놀부유황오리진흙구이(잠실점)', discovered_via='directory:x', context='유황') is None
    assert upsert_venue(db, '봉일스파랜드', discovered_via='directory:x', context='') is not None
    assert upsert_venue(db, 'Some Hotel Josun') is not None  # trusted seed path is not gated
    assert db.execute('SELECT COUNT(*) FROM venues').fetchone()[0] == 2


def test_profile_visitseoul_category_restriction():
    from hanset_active.config import load_profile
    import re
    for d in load_profile()['directories']:
        if d['id'].startswith('visitseoul'):
            ci = d['child_include']
            assert any(re.search(x, 'https://korean.visitseoul.net/sitemap/entertainment_kr_sitemap.xml') for x in ci)
            assert not any(re.search(x, 'https://korean.visitseoul.net/sitemap/restaurants_kr_sitemap.xml') for x in ci)
            assert not any(re.search(x, 'https://english.visitseoul.net/sitemap/shopping_en_sitemap.xml') for x in ci)
