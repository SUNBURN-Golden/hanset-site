"""Offline Stage C integration: real guarded HTTP and persisted service evidence."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import pytest
from hanset_active import venues
from hanset_active.discover.search import Hit, SearchProvider
from hanset_active.executor import Executor, TaskFailure
from hanset_active.runner import run
from hanset_active.scheduler import enqueue
from hanset_active.report import report


def page(price='25000',extra='',name='Aurora Sauna'):
    return f'<html><title>{name}</title><main><h1>{name}</h1><p>Seoul sauna spa hotel opening 2026-10-20</p><p>Day pass {price} KRW</p><p>Opening hours 09:00 ~ 22:00</p><p>Tattoos allowed</p><p>Foreigners welcome</p><p>Sauna 90°C</p>{extra}<p>Official visitor information for guests visiting this venue in Seoul.</p></main></html>'


class FixtureProvider(SearchProvider):
    name='fixture'
    def __init__(self,server,bot):
        self.server=server
        super().__init__(bot.fetch,bot.db,clock=bot.clock,endpoint=server.base+'/search')
    def parse(self,result,limit):
        return [Hit(**item) for item in json.loads(result.text)]
    def search(self,query,lang='ko',limit=10):
        # Mention searches discover a source on a second local hostname.
        target=self.server.base.replace('127.0.0.1','localhost')+'/second' if 'Meadow' in query else self.server.base+'/venue'
        from urllib.parse import urlencode
        self.server.add('/search?'+urlencode(self.params(query,lang,limit)),json.dumps([{'url':target,'title':'Seoul sauna spa hotel','website':target}]),headers={'Content-Type':'application/json'})
        return super().search(query,lang,limit)


def setup_profile(bot,tmp_path,server):
    p=bot.settings.profile
    p['regions']=p['regions'][:1]
    p['venue_types']=p['venue_types'][:1]
    p['languages']=['en']
    p['query_templates']=['{type_term} {region_term}']
    p['directories']=[{'id':'fixture','kind':'html_list','url':server.base+'/list','max_pages':2}]
    path=tmp_path/'profile.json'
    path.write_text(json.dumps(p))
    bot.settings.profile_path=path
    return path


def counts(db):
    return {t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('raw_documents','extractions','labels','observations','venue_fields','field_changes','gaps')}


def test_discovery_pagination_idempotence_and_change(bot,server,tmp_path):
    setup_profile(bot,tmp_path,server)
    venues.seed(bot.db,[{'name':'Known Sauna','website':'https://known.test'}],clock=bot.clock)
    extra='<a href="/hours">Opening hours</a><p>Meadow Sauna</p>'
    server.add('/venue',page(extra=extra))
    server.add('/second',page(name='Meadow Sauna'))
    server.add('/hours',page(extra='<a href="/en/guide">English guide</a>'))
    server.add('/en/guide',page())
    server.add('/list','<main><h1>Seoul sauna spa directory listing</h1><p>'+('Visitor directory information. '*6)+'</p><a href="/venue">Aurora Sauna Seoul</a><a rel="next" href="/list?page=2">Next</a></main>')
    server.add('/list?page=2','<main><h1>Seoul sauna spa directory listing second page</h1><p>'+('Other visitor directory information. '*6)+'</p><a href="/hours">Sauna opening hours</a></main>')
    # Keep the frozen known source from being requested; it is identity only.
    result=run(bot,max_requests=60,max_minutes=2,providers=[FixtureProvider(server,bot)],initialize=False)
    assert result['completed']==0
    enqueue(bot.db,kind='search',payload={'query':'Seoul sauna'})
    enqueue(bot.db,kind='directory',payload=bot.settings.profile['directories'][0],task_class='backfill')
    result=run(bot,max_requests=60,max_minutes=2,providers=[FixtureProvider(server,bot)],initialize=False)
    assert result['completed']>0
    metrics=report(bot.db)
    assert metrics['new_venues']['count']>=2
    assert metrics['new_valid_sources']['count']>=2
    assert any('/list?page=2'==x['path'] for x in server.log)
    assert metrics['gaps_filled']['count']>0
    assert metrics['gaps_filled']['examples'][0]['evidence_url'].startswith('http://')
    assert any('search:' in r['discovered_via'] for r in bot.db.execute('SELECT * FROM tasks'))
    before=counts(bot.db)
    run(bot,max_requests=60,max_minutes=2,providers=[FixtureProvider(server,bot)],initialize=False)
    assert counts(bot.db)==before
    assert report(bot.db)['duplicate_check']['total']==0
    server.add('/venue',page('31000',extra=extra))
    bot.clock.sleep(1)
    enqueue(bot.db,kind='fetch',url=server.base+'/venue',task_class='freshness',dedupe_key='second-pass')
    run(bot,max_requests=10,max_minutes=1,providers=[],initialize=False)
    changes=bot.db.execute("SELECT * FROM field_changes WHERE field='price_day_pass'").fetchall()
    assert changes and '25000' in changes[-1]['old_value'] and '31000' in changes[-1]['new_value']


@pytest.mark.skipif(not shutil.which('pdftotext'),reason='pdftotext unavailable')
def test_pdf_pipeline(bot,server,documents):
    server.add('/venue',page(extra='<a href="/rates.pdf">Price PDF</a>'))
    server.add('/rates.pdf',documents['pdf'],headers={'Content-Type':'application/pdf'})
    enqueue(bot.db,kind='trial',url=server.base+'/venue')
    run(bot,max_requests=12,max_minutes=1,providers=[],initialize=False)
    assert report(bot.db)['pdfs_collected']==1
    assert bot.db.execute("SELECT count(*) FROM observations WHERE source_type='attachment'").fetchone()[0]>0
    raw=bot.db.execute("SELECT blob_path FROM raw_documents WHERE content_type='application/pdf'").fetchone()
    assert Path(raw[0]).read_text().startswith('[page 1]')


@pytest.mark.skipif(not shutil.which('tesseract'),reason='tesseract unavailable')
def test_ocr_pipeline(bot,server,documents):
    server.add('/venue',page(extra='<img src="/price.png" alt="price timetable">'))
    server.add('/price.png',documents['png'],headers={'Content-Type':'image/png'})
    enqueue(bot.db,kind='trial',url=server.base+'/venue')
    run(bot,max_requests=12,max_minutes=1,providers=[],initialize=False)
    if not report(bot.db)['images_ocred']: pytest.skip('OCR language data unavailable')
    assert bot.db.execute("SELECT count(*) FROM observations WHERE source_type='attachment'").fetchone()[0]>0
    assert report(bot.db)['images_ocred']==1


def test_retries_structure_and_caps(bot,server):
    server.add('/broken','broken',status=500)
    enqueue(bot.db,kind='fetch',url=server.base+'/broken')
    run(bot,max_requests=10,max_minutes=100,providers=[],initialize=False)
    assert bot.db.execute('SELECT attempts,state FROM tasks').fetchone()[:]==(3,'dead')
    assert bot.db.execute('SELECT state FROM failures').fetchone()[0]=='dead'
    server.add('/venue',page())
    enqueue(bot.db,kind='fetch',url=server.base+'/venue')
    run(bot,max_requests=5,max_minutes=1,providers=[],initialize=False)
    server.add('/venue','<title>Aurora Sauna</title><main><section><article><div><span>'+('Sauna venue visitor guidance and information. '*8)+'</span></div></article></section></main>')
    enqueue(bot.db,kind='fetch',url=server.base+'/venue',dedupe_key='changed-structure')
    run(bot,max_requests=5,max_minutes=1,providers=[],initialize=False)
    assert report(bot.db)['structure_alerts']['count']>=1
    for i in range(5):
        server.add('/cap'+str(i),page())
        enqueue(bot.db,kind='fetch',url=server.base+'/cap'+str(i))
    before=len(server.log)
    result=run(bot,max_requests=1,max_minutes=1,providers=[],initialize=False)
    assert result['requests']==1 and len(server.log)-before==1
    assert bot.db.execute("SELECT count(*) FROM tasks WHERE state='pending'").fetchone()[0]>0


def test_browser_fallback_and_terminal(bot,server,monkeypatch):
    from hanset_active.extract import browser
    from hanset_active.fetcher import FetchResult
    seen=[]
    def render(fetcher,url):
        seen.append(url)
        return FetchResult(url,'ok',status=200,content=page().encode(),content_type='text/html',fetch_method='browser')
    monkeypatch.setattr(browser,'fetch',render)
    server.add('/app','<div id="app"></div><script src="x"></script>')
    server.add('/blocked','<main>Just a moment</main>')
    for url in ('/app','/blocked'): enqueue(bot.db,kind='fetch',url=server.base+url)
    run(bot,max_requests=10,max_minutes=1,providers=[],initialize=False)
    assert seen==[server.base+'/app']
    assert bot.db.execute("SELECT state FROM tasks WHERE url LIKE '%/blocked'").fetchone()[0]=='dead'


def test_trial_spa_shell_uses_browser_and_text_trial_does_not(bot,server,monkeypatch):
    from hanset_active.extract import browser
    from hanset_active.extract.html import extract
    from hanset_active.fetcher import FetchResult, detect_outcome
    shell='<div id="app"></div><script src="x"></script>'
    thin='<div id="app"><main>'+('Sauna spa hotel day pass information for guests. '*2)+'</main></div><script src="x"></script>'
    blank='<main></main>'
    plain=page()
    # 80–199 chars of main text is outcome ok but still an app shell. Trials
    # must not pay for that render; only an empty_body shell does.
    assert detect_outcome(shell.encode(),'text/html',200)=='empty_body' and browser.needs_browser(shell)
    assert detect_outcome(thin.encode(),'text/html',200)=='ok' and browser.needs_browser(thin) and 80<=len(extract(thin)['text'])<200
    assert detect_outcome(blank.encode(),'text/html',200)=='empty_body' and not browser.needs_browser(blank)
    assert detect_outcome(plain.encode(),'text/html',200)=='ok' and not browser.needs_browser(plain)
    seen=[]
    def render(fetcher,url):
        seen.append(url)
        return FetchResult(url,'ok',status=200,content=plain.encode(),content_type='text/html',fetch_method='browser')
    monkeypatch.setattr(browser,'fetch',render)
    server.add('/app',shell)
    server.add('/thin',thin)
    server.add('/thin-fetch',thin)
    server.add('/blank',blank)
    server.add('/venue',plain)
    enqueue(bot.db,kind='trial',url=server.base+'/app')
    enqueue(bot.db,kind='trial',url=server.base+'/thin')
    enqueue(bot.db,kind='fetch',url=server.base+'/thin-fetch')
    enqueue(bot.db,kind='trial',url=server.base+'/blank')
    enqueue(bot.db,kind='trial',url=server.base+'/venue')
    run(bot,max_requests=20,max_minutes=1,providers=[],initialize=False)
    assert set(seen)=={server.base+'/app',server.base+'/thin-fetch'}
    methods={r['url']:r['fetch_method'] for r in bot.db.execute('SELECT url,fetch_method FROM raw_documents')}
    assert methods[server.base+'/app']=='browser'
    assert methods[server.base+'/thin-fetch']=='browser'
    assert methods[server.base+'/thin']=='http'
    assert methods[server.base+'/venue']=='http'
    assert server.base+'/blank' not in methods
    states={r['url']:r['state'] for r in bot.db.execute('SELECT url,state FROM tasks')}
    assert states[server.base+'/app']=='done'
    assert states[server.base+'/thin']=='done'
    assert states[server.base+'/thin-fetch']=='done'
    assert states[server.base+'/venue']=='done'
    assert states[server.base+'/blank']=='failed'


def test_kill_resume(bot,server,tmp_path):
    server.add('/venue',page())
    tid=enqueue(bot.db,kind='fetch',url=server.base+'/venue')
    script='''
import sys
from pathlib import Path
from hanset_active.config import Settings
from hanset_active.db import connect
from hanset_active.fetcher import Fetcher
from hanset_active.runner import run
s=Settings(data_dir=Path(sys.argv[1]),testing=True,allow_private_hosts=True,min_interval=0,global_gap=0)
f=Fetcher(s,connect(s.data_dir/'test.sqlite3'))
run(f,max_requests=5,max_minutes=1,providers=[],initialize=False)
'''
    env=dict(os.environ,HANSET_ACTIVE_TEST_EXECUTOR_SLEEP='30')
    process=subprocess.Popen([sys.executable,'-c',script,str(bot.settings.data_dir)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        timeout=time.monotonic()+10
        while time.monotonic()<timeout:
            if bot.db.execute('SELECT count(*) FROM raw_documents').fetchone()[0]: break
            if process.poll() is not None:
                pytest.fail(process.communicate()[1].decode())
            time.sleep(.02)
        else: pytest.fail('Executor did not reach persisted crash boundary')
        process.kill(); process.wait(timeout=5)
        assert process.returncode==-signal.SIGKILL
        result=run(bot,max_requests=15,max_minutes=1,providers=[],initialize=False)
        assert result['completed']>0
        assert bot.db.execute('SELECT state FROM tasks WHERE task_id=?',(tid,)).fetchone()[0]=='done'
        assert bot.db.execute('SELECT count(*) FROM raw_documents').fetchone()[0]==1
        assert report(bot.db)['duplicate_check']['total']==0
    finally:
        if process.poll() is None: process.kill(); process.wait()


def test_gap_labels_with_coverage(bot,server):
    from hanset_active import gaps
    from hanset_active.labeling import store_observation
    from hanset_active.extract.fields import extract
    vid=venues.upsert_venue(bot.db,'Aurora Sauna',website=server.base,clock=bot.clock)
    venue=bot.db.execute('SELECT * FROM venues WHERE venue_id=?',(vid,)).fetchone()
    rows={r['field']:r for r in gaps.ensure_gaps(bot.db,vid,clock=bot.clock)}
    coverage=[{'url':server.base+p,'outcome':'ok','text':'Visitor guidance only','counterpart':p=='/en/','attachment':p=='/guide.pdf'} for p in ('/','/en/','/guide.pdf')]
    assert gaps.resolve(bot.db,rows['closures']['gap_id'],coverage,exhausted=True)=='absent_in_source'
    assert gaps.resolve(bot.db,rows['hours']['gap_id'],[{'url':server.base,'outcome':'blocked'}],exhausted=True)=='inaccessible'
    assert gaps.resolve(bot.db,rows['tattoo_policy']['gap_id'],[{'url':server.base,'outcome':'ok','text':'Tattoo policy 1 pending'}],exhausted=True)=='not_extracted'
    for amount,path in (('25000','/a'),('31000','/b')):
        store_observation(bot.db,venue['venue_key'],extract('Day pass '+amount+' KRW')[0],source_url=server.base+path,source_type='own_site',clock=bot.clock)
    assert gaps.resolve(bot.db,rows['price_day_pass']['gap_id'])=='conflict'


def test_pagination_checkpoint_resumes_without_refetch(bot,server,monkeypatch):
    monkeypatch.setattr('hanset_active.fetcher.is_korean_news', lambda url: True)
    server.add('/list','<main><h1>Seoul sauna directory listing</h1><p>'+('Visitor directory information. '*6)+'</p><a rel="next" href="/list?page=2">Next</a></main>')
    server.add('/list?page=2','<main><h1>Seoul sauna directory listing page two</h1><p>'+('Visitor directory information continues here. '*6)+'</p></main>')
    enqueue(bot.db,kind='directory',payload={'id':'local','kind':'html_list','url':server.base+'/list','max_pages':2},task_class='backfill')
    first=run(bot,max_requests=2,max_minutes=1,providers=[],initialize=False)
    assert first['state']=='budget_reached'
    assert bot.db.execute("SELECT count(*) FROM run_checkpoints WHERE kind LIKE 'fetched:%'").fetchone()[0]==1
    run(bot,max_requests=10,max_minutes=1,providers=[],initialize=False)
    assert sum(x['path']=='/list' for x in server.log)==1
    assert sum(x['path']=='/list?page=2' for x in server.log)==1
    assert bot.db.execute("SELECT count(*) FROM tasks WHERE state='pending'").fetchone()[0]==0


def test_sigterm_finishes_current_task(bot,server,monkeypatch):
    for route in ('/first','/second'):
        server.add(route,page())
        enqueue(bot.db,kind='fetch',url=server.base+route)
    original=Executor.execute
    finished=[]
    def stop_after_current(self,task):
        os.kill(os.getpid(),signal.SIGTERM)
        original(self,task)
        finished.append(task['task_id'])
    monkeypatch.setattr(Executor,'execute',stop_after_current)
    result=run(bot,max_requests=10,max_minutes=1,providers=[],initialize=False)
    assert result['state']=='stopped' and len(finished)==1
    assert bot.db.execute('SELECT state FROM tasks WHERE task_id=?',(finished[0],)).fetchone()[0]=='done'
    assert bot.db.execute("SELECT count(*) FROM tasks WHERE state='running'").fetchone()[0]==0
    assert bot.db.execute("SELECT count(*) FROM tasks WHERE state='pending'").fetchone()[0]>0


def test_daily_body_cap_and_freshness(bot,server,tmp_path):
    from hanset_active.runner import bootstrap
    setup_profile(bot,tmp_path,server)
    bot.settings.body_cap=1
    server.add('/venue',page())
    server.add('/another',page())
    enqueue(bot.db,kind='fetch',url=server.base+'/venue')
    enqueue(bot.db,kind='fetch',url=server.base+'/another')
    run(bot,max_requests=20,max_minutes=1,providers=[],initialize=False)
    assert sum(x['path']!='/robots.txt' for x in server.log)==1
    assert bot.db.execute("SELECT used FROM budgets_daily WHERE kind='bodies'").fetchone()[0]==1
    bot.clock.sleep(3*86400+1)
    bootstrap(bot)
    assert bot.db.execute("SELECT count(*) FROM tasks WHERE task_class='freshness'").fetchone()[0]>0


def test_redirect_identity_is_reused(bot, server):
    identity, effective = server.base + '/kr', server.base + '/kr/'
    content = page(name='Example Sauna')
    server.add('/kr', '', 301, {'Location': '/kr/'})
    server.add('/kr/', content)
    executor = Executor(bot, providers=[])

    first = executor.get(identity)
    assert first.ok and first.url == effective and first.identity_url == identity
    assert effective in executor.documents
    observations = bot.db.execute('SELECT DISTINCT source_url FROM observations').fetchall()
    assert [row[0] for row in observations] == [effective]

    server.add('/kr', content)
    second = executor.get(effective)
    assert second.ok and second.url == identity and second.identity_url == identity

    raw = bot.db.execute('SELECT url, canonical_url FROM raw_documents').fetchall()
    assert [tuple(row) for row in raw] == [(effective, identity)]
    baselines = bot.db.execute('SELECT page_type FROM structure_baselines').fetchall()
    assert [row[0] for row in baselines] == ['/kr']
    assert not bot.db.execute('SELECT 1 FROM structure_alerts').fetchone()
    assert bot.db.execute('SELECT url FROM sources').fetchone()[0] == identity
    trials = bot.db.execute('SELECT url FROM source_trials').fetchall()
    assert [row[0] for row in trials] == [identity]
    assert [r['path'] for r in server.log] == ['/kr', '/kr/', '/kr']


def test_redirect_checkpoint_returns_effective_url(bot, server):
    bot.facts_only = True
    identity, effective = server.base + '/kr', server.base + '/kr/'
    server.add('/kr', '', 301, {'Location': '/kr/'})
    server.add('/kr/', page(name='Example Sauna'))
    task_id = enqueue(bot.db, kind='fetch', url=identity)
    executor = Executor(bot, providers=[])
    executor.task = dict(bot.db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone())
    assert executor.get(identity).ok

    resumed = Executor(bot, providers=[])
    resumed.task = executor.task
    result = resumed.get(identity)

    assert result.ok and result.url == effective and result.identity_url == identity
    assert effective in resumed.documents
    assert [r['path'] for r in server.log] == ['/kr', '/kr/']


def test_canonical_elsewhere_is_not_own_fact(bot,server):
    server.add('/venue',page(extra='<link rel="canonical" href="https://original.test/info">'))
    enqueue(bot.db,kind='fetch',url=server.base+'/venue')
    run(bot,max_requests=8,max_minutes=1,providers=[],initialize=False)
    assert bot.db.execute("SELECT count(*) FROM observations WHERE label_status='fact'").fetchone()[0]==0
    assert bot.db.execute("SELECT count(*) FROM observations WHERE label_status='source_claim'").fetchone()[0]>0
