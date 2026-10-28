"""Task dispatcher and evidence pipeline. All HTTP passes through one fetcher."""
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urljoin, unquote
from bs4 import BeautifulSoup
from . import evaluator, gaps, labeling, venues
from .db import deterministic_id
from .fetcher import FetchResult
from .discover import Discovery, directories, links, mentions
from .discover.search import Nominatim, Wikipedia, Brave, DDGHtml
from .extract import html, pdf, ocr, feed, sitemap, browser, fields
from .scheduler import enqueue
from .util.hashing import content_hash
from .util.pii import strip_pii
from .util.timeutil import kst_now
from .util.urls import registrable_domain, is_excluded

class BudgetExhausted(RuntimeError):
    pass

class TaskFailure(RuntimeError):
    def __init__(self, reason, *, terminal=False, retry_after=None):
        super().__init__(reason)
        self.terminal, self.retry_after = terminal, retry_after


def structure_signature(content, field_ids):
    """A comparable tag-path set plus field ids; persist its hash and tokens."""
    soup = BeautifulSoup(content, 'lxml')
    main = soup.select_one('main,article,[role="main"]') or soup.body or soup
    paths = set()
    for node in main.find_all(True):
        if node.name in ('script', 'style'): continue
        names = [node.name]
        for parent in node.parents:
            if parent is main: break
            if parent.name: names.append(parent.name)
        paths.add('/'.join(reversed(names)))
    tokens = sorted(paths | {'field:'+f for f in field_ids})
    return json.dumps({'hash': deterministic_id(tokens), 'tokens': tokens}, sort_keys=True)

class Executor:
    def __init__(self, fetcher, *, providers=None, max_requests=100, deadline=None):
        self.fetcher, self.db, self.clock = fetcher, fetcher.db, fetcher.clock
        self.deadline, self.max_requests = deadline, max_requests
        self.start_requests = self.db.execute('SELECT count(*) FROM request_log').fetchone()[0]
        self.run_id = None
        self.task = None
        self.documents = {}
        for row in self.db.execute('SELECT structured FROM extractions'):
            saved = json.loads(row[0] or '{}').get('_active_page')
            if saved: self.documents[saved['url']] = saved
        self.context = Discovery(fetcher, getter=self.get, budget=max_requests)
        self.providers = providers if providers is not None else [p(self.context.get, self.db, clock=self.clock) for p in (Nominatim, Wikipedia, Brave, DDGHtml)]
        self.heartbeat = lambda: None
        self._original_request = fetcher._request
        self._original_fetch = fetcher.fetch

    @property
    def requests(self):
        return self.db.execute('SELECT count(*) FROM request_log').fetchone()[0] - self.start_requests

    def guarded_request(self, *args, **kwargs):
        self.heartbeat()
        if self.requests >= self.max_requests or (self.deadline is not None and self.clock.monotonic() >= self.deadline):
            raise BudgetExhausted()
        previous = self.db.execute('SELECT coalesce(max(request_id),0) FROM request_log').fetchone()[0]
        try:
            return self._original_request(*args, **kwargs)
        finally:
            if self.run_id:
                with self.db:
                    self.db.execute('UPDATE request_log SET run_id=? WHERE request_id>?',(self.run_id,previous))

    def install(self):
        self.fetcher._request = self.guarded_request

    def close(self):
        self.fetcher._request = self._original_request

    def get(self, url, *, kind='body', **kwargs):
        task = self.task or {}
        checkpoint_id = deterministic_id('fetched',task.get('task_id'),url)
        saved = self.db.execute('SELECT cursor FROM run_checkpoints WHERE checkpoint_id=?',(checkpoint_id,)).fetchone() if task.get('task_id') else None
        if saved:
            raw = self.db.execute('SELECT * FROM raw_documents WHERE raw_id=?',(saved[0],)).fetchone()
            if raw and Path(raw['blob_path']).is_file():
                return FetchResult(raw['url'],'ok',status=raw['http_status'],content=Path(raw['blob_path']).read_bytes(),
                    content_type=raw['content_type'],content_hash=raw['content_hash'],fetched_at=raw['fetched_at'],fetch_method=raw['fetch_method'])
        result = self._original_fetch(url, kind=kind, _browser_shell=True,
            parent_url=task.get('parent_url'), task_id=task.get('task_id'),
            discovered_via=task.get('discovered_via'), **kwargs)
        if task.get('kind')!='trial' and result.outcome in ('ok', 'empty_body') and 'html' in result.content_type and browser.needs_browser(result.content):  # browser subrequests are budgeted: not for cheap trials
            # A blocked/login response never reaches this branch. Only one
            # rendered attempt is allowed, with every subrequest budgeted.
            result = browser.fetch(self.fetcher, result.url)
        if result.ok:
            raw_id = self.process(result)
            if raw_id and task.get('task_id'):
                with self.db:
                    self.db.execute('INSERT OR IGNORE INTO run_checkpoints(checkpoint_id,task_id,kind,cursor,updated_at) VALUES (?,?,?,?,?)',
                        (checkpoint_id,task['task_id'],'fetched:'+deterministic_id(url),raw_id,kst_now(self.clock).isoformat()))
        return result

    def _extract(self, result):
        ct, data, url = result.content_type, result.content, result.url
        if 'pdf' in ct: return pdf.extract(data, url)
        if ct.startswith('image/'):
            if not ocr.is_image_notice(data, filename=url, context=(self.task or {}).get('discovered_via', '')):
                return dict(extractor='ocr', extractor_version=ocr.VERSION, status='not_notice', text='')
            return ocr.extract(data, url)
        if 'json' in ct or 'rss' in ct or 'atom' in ct: return feed.extract(data, url)
        if 'xml' in ct:
            if re.search(rb'<(?:\w+:)?(?:sitemapindex|urlset)\b', data): return sitemap.extract(data, url)
            return feed.extract(data, url)
        document = (browser if result.fetch_method == 'browser' else html).extract(data, url)
        canonical = BeautifulSoup(data,'lxml').select_one('link[rel=canonical][href]')
        if canonical: document['canonical_url'] = urljoin(url,canonical['href'])
        return document

    def process(self, result):
        doc = self._extract(result)
        text = strip_pii(doc.get('text', ''))
        if is_excluded(result.url, text, json.dumps(doc, ensure_ascii=False)): return
        task = self.task or {}
        unavailable = doc.get('status') in ('unavailable', 'not_notice')
        domain = registrable_domain(result.url)
        now = kst_now(self.clock).isoformat()
        source_id = deterministic_id(domain)
        raw_hash = result.content_hash or content_hash(result.content, result.content_type)
        raw_id = deterministic_id(result.url, raw_hash)
        # Binary originals never touch disk: only sanitized extracted text does.
        blob = text.encode() if ('pdf' in result.content_type or result.content_type.startswith('image/')) else strip_pii(result.content)
        blob_path = self.fetcher.settings.data_dir/'raw'/raw_hash[:2]/raw_hash
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        if not blob_path.exists():
            temporary = blob_path.with_suffix('.tmp')
            temporary.write_bytes(blob)
            temporary.replace(blob_path)
        page = dict(doc, url=result.url, outcome='ok', unavailable=unavailable,
                    attachment=doc['extractor'] in ('pdf','ocr'), counterpart=bool(re.search(r'/en/|/ko/', result.url)))
        self.documents[result.url] = page
        eid = deterministic_id(raw_id, doc['extractor'], doc['extractor_version'])
        items = labeling.labels(text, content_type=result.content_type)
        page_type = items[0]['value']
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO raw_documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (raw_id,result.url,result.url,domain,result.fetched_at or now,result.status,result.content_type,raw_hash,len(blob),str(blob_path),source_id,task.get('parent_url'),task.get('task_id'),result.fetch_method,doc['extractor_version'],'ok'))
            self.db.execute('INSERT OR IGNORE INTO extractions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (eid,raw_id,doc['extractor'],doc['extractor_version'],page_type,doc.get('lang'),doc.get('title',''),content_hash(text),json.dumps({**doc.get('structured',{}),'_active_page':page},ensure_ascii=False),json.dumps(doc.get('mentions',[]),ensure_ascii=False),json.dumps(doc.get('links',[]),ensure_ascii=False),json.dumps(doc.get('dates',[])),json.dumps(doc.get('amounts',[])),now))
        # A persisted raw insert is a safe crash boundary. The task remains leased.
        if self.fetcher.settings.testing:
            delay = float(os.environ.get('HANSET_ACTIVE_TEST_EXECUTOR_SLEEP', '0'))
            if delay: time.sleep(min(delay, 60))
        directory_detail = directories.detail(result.content,result.url,self.context.profile) if doc['extractor'] in ('html','browser') else None
        rejected_directory = bool(directory_detail and directory_detail['low_prior'] and
            evaluator.evaluate(result.url,[doc],venue_name=directory_detail['name'],profile=self.context.profile)['decision']!='registered')
        directory_venue = None
        if directory_detail and re.search(r'/[A-Z]{2}N\d+/?$',urlsplit(result.url).path):
            directory_detail = None  # editorial article (…N<id>), not a venue page (…P<id>)
        if directory_detail and directory_detail['name'] and not rejected_directory:
            directory_venue = venues.upsert_venue(self.db,directory_detail['name'],website=directory_detail['website'],
                discovered_via='directory:'+directory_detail['directory_id'],clock=self.clock,
                context=(directory_detail.get('category','') or '')+' '+doc.get('title','')+' '+unquote(urlsplit(result.url).path))
            page['directory_address']=directory_detail['address']
            if directory_detail['website'] and directory_venue:
                trial=self.context.candidate(directory_detail['website'],text=directory_detail['name'],via='directory:'+directory_detail['directory_id'],parent_url=result.url,low_prior=True)
                if trial:
                    with self.db: self.db.execute('UPDATE tasks SET venue_id=? WHERE task_id=?',(directory_venue,trial))
        venue = self.db.execute('SELECT * FROM venues WHERE venue_id=?', (directory_venue or task.get('venue_id'),)).fetchone()
        if venue is None:
            venue = next((v for v in self.db.execute('SELECT * FROM venues') if domain in json.loads(v['domains'] or '[]')), None)
        name = venue['name'] if venue else ''
        if not name:
            entities = [m['name'] for m in doc.get('mentions',[]) if m.get('kind') in ('LocalBusiness','HealthClub','SportsActivityLocation','Hotel','DaySpa')]
            if entities: name = entities[0]
            elif page_type != 'directory_listing' and re.search(r'sauna|spa|사우나|찜질방|CrossFit|HYROX', doc.get('title',''), re.I):
                name = doc['title'].split('|')[0].strip()
        source = self.db.execute('SELECT * FROM sources WHERE domain=?', (domain,)).fetchone()
        if not source or source['state'] not in ('registered','deprioritized','inaccessible') or (name and source['state']=='deprioritized' and source['reason']=='rule_scores'):
            old_texts = [(r['domain'], Path(r['blob_path']).read_text(errors='replace')) for r in self.db.execute('SELECT domain,blob_path FROM raw_documents WHERE domain<>?',(domain,)) if Path(r['blob_path']).is_file()]
            scores = evaluator.evaluate(result.url,[doc],venue_name=name,existing_texts=old_texts, profile=self.context.profile,
                seed_domains=[r[0] for r in self.db.execute('SELECT domain FROM seed_domains')])
            with self.db:
                self.db.execute('INSERT INTO sources(source_id,domain,url,state,reason,quality,first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(domain) DO UPDATE SET state=excluded.state,reason=excluded.reason,quality=excluded.quality,last_seen_at=excluded.last_seen_at',
                    (source_id,domain,result.url,scores['decision'],scores['reason'],scores['total'],now,now))
                self.db.execute('INSERT OR IGNORE INTO source_trials VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (deterministic_id(source_id,result.url),source_id,result.url,*(scores[k] for k in ('relevance','novelty','extractability','original','total','decision','reason')),now))
            source = self.db.execute('SELECT * FROM sources WHERE domain=?',(domain,)).fetchone()
        if rejected_directory: return raw_id
        if name and venue is None and not directory_detail:
            vid = venues.upsert_venue(self.db,name,website=result.url,discovered_via=task.get('discovered_via',''),clock=self.clock,context=doc.get('title',''))
            venue = self.db.execute('SELECT * FROM venues WHERE venue_id=?',(vid,)).fetchone() if vid else None
        # Registered relevance alone does not prove a page is an original source.
        original = evaluator.evaluate(result.url,[doc],venue_name=name,profile=self.context.profile)['original']
        if original and not directory_detail:
            with self.db: self.db.execute("UPDATE sources SET source_type='own_site' WHERE source_id=?",(source_id,))
        source_type = 'aggregator' if evaluator.is_aggregator(result.url,self.context.profile) else 'own_site' if venue and (original or source['source_type']=='own_site') else 'third_party'
        if directory_detail: source_type = 'official_directory'
        if venue and doc['extractor'] in ('pdf','ocr') and domain in json.loads(venue['domains'] or '[]'):
            source_type = 'attachment'
        labeling.store_labels(self.db,raw_id,eid,labeling.labels(text,source_type=source_type,content_type=result.content_type,
            hosted_on_own_domain=source_type=='attachment',ocr_confidence=doc.get('mean_confidence')),self.clock)
        evidence = fields.extract_document(doc)
        if directory_detail:
            for field in ('name','address','website'):
                value=directory_detail[field]
                if value:
                    evidence.append(dict(field=field,value=value,unit=None,quote=value[:240],locator={'selector':'directory:'+field},
                        rule_id='directory.'+field+'.v1',extractor_version='1.0.0',ambiguous=False))
        if venue:
            for item in evidence:
                oid = labeling.store_observation(self.db,venue['venue_key'],item,source_url=result.url,source_type=source_type,
                    raw_id=raw_id,extraction_id=eid,clock=self.clock,own_domain=venue['website'])
                venues.apply_observation(self.db,venue['venue_id'],oid,clock=self.clock)
            gaps.ensure_gaps(self.db,venue['venue_id'],clock=self.clock)
            if directory_detail:
                from .discover.reverse import followups
                for gap in self.db.execute('SELECT * FROM gaps WHERE venue_id=?',(venue['venue_id'],)).fetchall():
                    for strategy in ('site_links','lang_counterpart','attachments','images'):
                        for tid in followups(self.db,venue,gap['field'],[page],strategy=strategy,clock=self.clock):
                            gaps.record_attempt(self.db,gap['gap_id'],strategy,task_id=tid,url=tid,state='pending',clock=self.clock)
        if doc['extractor'] in ('html','browser'):
            self.check_structure(source_id,urlsplit(result.url).path or '/',raw_id,result.content,{e['field'] for e in evidence},page)
        state = source['state']
        if state == 'registered' and not directory_detail:
            links.discover(self.context,doc,parent_url=result.url,venue_id=venue['venue_id'] if venue else None,depth=task.get('depth',0),source_state=state)
            mentions.discover(self.context,doc,parent_url=result.url,depth=task.get('depth',0))
            # Sitemap adapters own filtering and their directory-wide cap.
            # Never register unfiltered entries while persisting the XML.
            for item in ([] if doc['extractor']=='sitemap' else doc.get('entries',[]) + doc.get('items',[])):
                target = item.get('url') or item.get('link')
                if target: self.context.candidate(urljoin(result.url,target),text=str(item),via=doc['extractor'],parent_url=result.url,low_prior=True)
        elif venue:
            links.discover(self.context,doc,parent_url=result.url,venue_id=venue['venue_id'],source_state='deprioritized' if directory_detail else state)
        with self.db:
            self.db.execute('UPDATE extractions SET structured=? WHERE extraction_id=?',
                (json.dumps({**doc.get('structured',{}),'_active_page':page},ensure_ascii=False),eid))
        if venue:
            self.update_gaps(venue['venue_id'])
            if page.get('needs_extractor_review'):
                with self.db:
                    self.db.execute("UPDATE gaps SET reason='needs_extractor_review' WHERE venue_id=?",(venue['venue_id'],))
        return raw_id

    def check_structure(self, source_id, page_type, raw_id, content, field_ids, page):
        signature = structure_signature(content,field_ids)
        old = self.db.execute('SELECT * FROM structure_baselines WHERE source_id=? AND page_type=?',(source_id,page_type)).fetchone()
        now = kst_now(self.clock).isoformat()
        with self.db:
            if old:
                a,b = set(json.loads(old['signature'])['tokens']), set(json.loads(signature)['tokens'])
                similarity = len(a&b)/max(1,len(a|b))
                if (json.loads(old['field_ids']) and not field_ids) or similarity < .5:
                    page['needs_extractor_review'] = True
                    self.db.execute('INSERT OR IGNORE INTO structure_alerts VALUES (?,?,?,?,?,?,?,?,?)',
                        (deterministic_id(source_id,page_type,raw_id),source_id,page_type,raw_id,old['signature'],signature,similarity,'needs_extractor_review',now))
                    self.db.execute("UPDATE sources SET reason='needs_extractor_review' WHERE source_id=?",(source_id,))
                    return
            self.db.execute('INSERT INTO structure_baselines VALUES (?,?,?,?,?,?) ON CONFLICT(source_id,page_type) DO UPDATE SET signature=excluded.signature,field_ids=excluded.field_ids,updated_at=excluded.updated_at',
                (deterministic_id(source_id,page_type),source_id,page_type,signature,json.dumps(sorted(field_ids)),now))

    def update_gaps(self, venue_id, *, terminal=False):
        venue = self.db.execute('SELECT domains FROM venues WHERE venue_id=?',(venue_id,)).fetchone()
        domains = json.loads(venue[0] or '[]')
        pages = [p for p in self.documents.values() if registrable_domain(p['url']) in domains]
        for gap in self.db.execute('SELECT * FROM gaps WHERE venue_id=?',(venue_id,)).fetchall():
            for _ in range(6):
                if gaps.resolve(self.db,gap['gap_id'],pages,clock=self.clock) != 'open': break
                new = gaps.advance(self.db,gap['gap_id'],pages,clock=self.clock)
                if new: break
            exhausted = terminal or self.db.execute("SELECT count(DISTINCT strategy) FROM gap_attempts WHERE gap_id=? AND state!='pending'",(gap['gap_id'],)).fetchone()[0] == 6
            gaps.resolve(self.db,gap['gap_id'],pages,exhausted=exhausted,clock=self.clock)

    def execute(self, task):
        self.task = dict(task)
        payload = json.loads(task.get('payload') or '{}')
        kind = task.get('kind') or 'fetch'
        if kind == 'search' or (kind == 'directory' and 'query' in payload):
            for provider in self.providers:
                if payload.get('provider') and payload['provider']!=provider.name: continue
                for hit in provider.search(payload.get('query',''),payload.get('lang','ko'),10):
                    self.context.candidate(hit.url,text=hit.title+' '+hit.snippet,name=hit.name,website=hit.website,
                        depth=task.get('depth',0),via='search:'+provider.name,parent_task_id=task['task_id'],low_prior=bool(hit.extra.get('low_prior')))
        elif kind == 'directory':
            directories.discover(self.context,payload)
        else:
            result = self.get(task['url'])
            if result is None or result.outcome == 'cap_reached': raise BudgetExhausted()
            if not result.ok:
                self.documents[task['url']] = dict(url=task['url'],outcome=result.outcome,text='')
                for row in self.db.execute('SELECT DISTINCT venue_id FROM gaps').fetchall(): self.update_gaps(row[0], terminal=result.outcome in ('blocked','login_wall','robots_disallowed'))
                if result.reason == 'excluded': return
                terminal = result.outcome in ('blocked','login_wall','robots_disallowed','denied')
                raise TaskFailure(result.outcome,terminal=terminal,retry_after=result.retry_after)
            if result.url not in self.documents and result.reason != 'excluded': return
        with self.db:
            self.db.execute("UPDATE gap_attempts SET state='done' WHERE task_id=?",(task['task_id'],))
        for row in self.db.execute('SELECT DISTINCT venue_id FROM gaps').fetchall(): self.update_gaps(row[0])
