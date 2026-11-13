"""Injectable, guarded search providers. No alternate-route block handling."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import os
import re
from urllib.parse import urlencode, urljoin, urlsplit, parse_qs, unquote
from bs4 import BeautifulSoup
from ..util.timeutil import kst_now
from ..util.pii import strip_pii
from ..util.urls import is_denied, is_excluded
from ..search import tavily as tavily_client
from ..search.budget import availability as budget_availability

DEFAULT_PROVIDERS = ('tavily','nominatim','wikipedia','brave','ddg_html')
_TAVILY_RECHECK = frozenset({
    'tavily_key_missing', 'tavily_key_invalid', 'tavily_budget_exhausted',
    'tavily_budget_unavailable', 'tavily_cooldown', 'tavily_clock_changed',
    'tavily_rate_limited',
})


class DiscoveryDeferred(RuntimeError):
    """Unfinished discovery; leave pending without adding a failure attempt."""
    def __init__(self, reason='search_provider_unavailable', next_at=None):
        super().__init__(reason)
        self.next_at = next_at


def provider_names(payload, defaults=None):
    """Keep explicit pins authoritative for both scheduler and executor."""
    configured=list(DEFAULT_PROVIDERS if defaults is None else defaults)
    if payload.get('provider'):
        return [payload['provider']] if payload['provider'] in configured else []
    if payload.get('kind') == 'osm_search':
        return [name for name in ('tavily','nominatim') if name in configured]
    return configured


def _provider_state(db, name, state, reason='', next_at=None, *, clock=None):
    with db:
        db.execute('INSERT INTO provider_state VALUES (?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET state=excluded.state,reason=excluded.reason,next_at=excluded.next_at,updated_at=excluded.updated_at',
                   (name,state,reason,next_at,kst_now(clock).isoformat()))


def provider_retry_at(db, names, *, clock=None):
    now = kst_now(clock)
    times = [r['next_at'] for r in db.execute('SELECT * FROM provider_state')
             if r['provider'] in names and r['next_at'] and r['next_at'] > now.isoformat()]
    return min(times) if times else (now + timedelta(minutes=10)).isoformat()

def usable_providers(db, *, clock=None, names=None):
    """Return provider names whose existing block/disable state permits work."""
    names=DEFAULT_PROVIDERS if names is None else names
    now=kst_now(clock).isoformat()
    states={r['provider']:r for r in db.execute('SELECT * FROM provider_state')}
    result=[]
    for name in names:
        row=states.get(name)
        blocked=row and row['state'] in ('disabled','inaccessible') and (not row['next_at'] or row['next_at']>now)
        if blocked and not (name=='tavily' and row['reason'] in _TAVILY_RECHECK):
            continue
        if name=='brave' and not os.environ.get('BRAVE_API_KEY'):
            continue
        if name=='tavily':
            reason=tavily_client.credential_status()
            retry=None
            if reason=='ok':
                available=budget_availability(clock=clock)
                reason,retry=available.reason,available.retry_at
            _provider_state(db,name,'ready' if reason=='ok' else 'inaccessible',reason,retry,clock=clock)
            if reason!='ok': continue
        result.append(name)
    return result


@dataclass
class Hit:
    url: str
    title: str=''
    snippet: str=''
    name: str=''
    address: str=''
    website: str=''
    extra: dict=field(default_factory=dict)

class SearchProvider:
    name='base'
    endpoint=''
    def __init__(self, getter, db, *, clock=None, endpoint=None):
        self.getter=getter;self.db=db;self.clock=clock
        self.endpoint=endpoint or self.endpoint
    def state(self,state,reason='',next_at=None):
        with self.db:
            self.db.execute('INSERT INTO provider_state VALUES (?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET state=excluded.state,reason=excluded.reason,next_at=excluded.next_at,updated_at=excluded.updated_at',(self.name,state,strip_pii(reason),next_at,kst_now(self.clock).isoformat()))
    def params(self,query,lang,limit): return {'q':query}
    def parse(self,result,limit): raise NotImplementedError
    def search(self,query,lang='ko',limit=10):
        self.completed=False
        row=self.db.execute('SELECT * FROM provider_state WHERE provider=?',(self.name,)).fetchone()
        if row and row['state'] in ('inaccessible','disabled') and (not row['next_at'] or row['next_at']>kst_now(self.clock).isoformat()): return []
        if is_excluded(query): return []
        if self.name=='brave' and not os.environ.get('BRAVE_API_KEY'):
            self.state('disabled','missing credential');return []
        try:
            request_options={'api_headers':{'X-Subscription-Token':os.environ['BRAVE_API_KEY']}} if self.name=='brave' else {}
            result=self.getter(self.endpoint+'?'+urlencode(self.params(query,lang,limit)),kind='api',**request_options)
            if result is None: return []
            if not result.ok or (self.name=='ddg_html' and re.search(r'anomaly|challenge|select all squares',result.text,re.I)):
                tomorrow=(kst_now(self.clock)+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0).isoformat()
                self.state('inaccessible',result.reason or result.outcome,tomorrow);return []
            hits=self.parse(result,limit)
            self.state('ready')
            self.completed=True
            return [h for h in hits if not is_denied(h.url) and not is_excluded(h.url,h.title,h.snippet,h.name)][:limit]
        except (ValueError,KeyError,TypeError,OSError) as exc:
            self.state('inaccessible',type(exc).__name__);return []


class Tavily(SearchProvider):
    """Adapt sanitized hints; never supply identity or fetched evidence."""
    name='tavily'
    endpoint=tavily_client.ENDPOINT

    def __init__(self, getter, db, *, clock=None, context=None, transport=None):
        super().__init__(getter,db,clock=clock)
        bound=getattr(getter,'__self__',None)
        self.context=context if context is not None else bound if hasattr(bound,'search_allowed') else None
        self.transport=transport
        self.completed=False
        self.reason='search_provider_unavailable'

    def search(self,query,lang='ko',limit=10):
        self.completed=False
        # Validate before availability loads credentials, including for bad jobs.
        if tavily_client.query_status(query)!='ok':
            self.reason='tavily_invalid_query'
            self.completed=True
            return []
        if not usable_providers(self.db,clock=self.clock,names=[self.name]):
            row=self.db.execute('SELECT reason FROM provider_state WHERE provider=?',(self.name,)).fetchone()
            # Manual block reasons are not copied to diagnostics.
            self.reason=row[0] if row and row[0] in _TAVILY_RECHECK else 'search_provider_unavailable'
            return []
        outcome=tavily_client.discover_query(query,clock=self.clock,transport=self.transport,
            dispatch_guard=self.context.search_allowed if self.context else None,
            on_dispatch=self.context.search_dispatch if self.context else None)
        self.reason=outcome.reason
        self.completed=outcome.status=='ok' or outcome.reason=='tavily_invalid_query'
        if outcome.reason=='tavily_run_limit': return []
        if outcome.status=='ok':
            self.state('ready','ok')
        else:
            available=budget_availability(clock=self.clock) if outcome.reason in _TAVILY_RECHECK else None
            retry=available.retry_at if available and available.retry_at else (kst_now(self.clock)+timedelta(minutes=10)).isoformat()
            self.state('inaccessible',outcome.reason,retry)
        return [Hit(lead.url,lead.title,lead.snippet,extra={'provider':self.name,'verification':'unverified'})
                for lead in outcome.leads if not is_denied(lead.url) and not is_excluded(lead.url,lead.title,lead.snippet)][:max(0,limit)]

class Nominatim(SearchProvider):
    name='nominatim';endpoint='https://nominatim.openstreetmap.org/search'
    def params(self,query,lang,limit):
        return {'q':query,'format':'jsonv2','extratags':1,'namedetails':1,'addressdetails':1,'accept-language':lang,'limit':min(limit,50)}
    def parse(self,result,limit):
        hits=[]
        for row in json.loads(result.text):
            if row.get('class',row.get('category')) not in ('leisure','amenity','shop','tourism'): continue
            tags=row.get('extratags') or {}; website=tags.get('website') or tags.get('contact:website','')
            name=row.get('name') or (row.get('namedetails') or {}).get('name') or row.get('display_name','').split(',')[0]
            if website: hits.append(Hit(website,name,row.get('display_name',''),name,strip_pii(str(row.get('address',''))),website,{'provider':self.name}))
        return hits

class Wikipedia(SearchProvider):
    name='wikipedia';endpoint='https://ko.wikipedia.org/w/api.php'
    def params(self,query,lang,limit): return {'action':'query','list':'search','srsearch':query,'srlimit':limit,'format':'json'}
    def parse(self,result,limit):
        return [Hit(f'https://{self.lang}.wikipedia.org/?curid={r["pageid"]}',r['title'],BeautifulSoup(r.get('snippet',''),'lxml').get_text(),extra={'low_prior':True}) for r in json.loads(result.text).get('query',{}).get('search',[])]
    def search(self,query,lang='ko',limit=10):
        self.lang=lang if lang in ('ko','en') else 'en'
        if 'wikipedia.org' in self.endpoint: self.endpoint=f'https://{self.lang}.wikipedia.org/w/api.php'
        return super().search(query,lang,limit)

class Brave(SearchProvider):
    name='brave';endpoint='https://api.search.brave.com/res/v1/web/search'
    def params(self,query,lang,limit): return {'q':query,'search_lang':lang,'count':limit}
    def parse(self,result,limit):
        return [Hit(r['url'],r.get('title',''),r.get('description','')) for r in json.loads(result.text).get('web',{}).get('results',[])]

class DDGHtml(SearchProvider):
    name='ddg_html';endpoint='https://html.duckduckgo.com/html/'
    def params(self,query,lang,limit): return {'q':query,'kl':'kr-kr' if lang=='ko' else 'us-en'}
    def parse(self,result,limit):
        soup=BeautifulSoup(result.text,'lxml');hits=[]
        for a in soup.select('a.result__a'):
            url=urljoin(self.endpoint,a.get('href','')); args=parse_qs(urlsplit(url).query)
            url=args.get('uddg',[url])[0]
            parent=a.find_parent(class_='result')
            snippet=parent.select_one('.result__snippet') if parent else None
            hits.append(Hit(url,a.get_text(' ',strip=True),snippet.get_text(' ',strip=True) if snippet else ''))
        return hits

def queries(profile):
    for region in profile['regions']:
        for kind in profile['venue_types']:
            for lang in profile['languages']:
                for term in kind[lang+'_terms']:
                    base={'type_term':term,'region_term':region[lang]}
                    for template in profile['query_templates']:
                        extras=profile.get('event_terms',[]) if '{event_term}' in template else [w for f in profile['fields'] for w in f['keywords_'+lang]] if '{field_term}' in template else ['']
                        for extra in extras: yield template.format(**base,field_term=extra,event_term=extra),lang

def discover(context,providers, *, limit=10):
    tasks=[]
    for query,lang in queries(context.profile):
        if not context.remaining: break
        for provider in providers:
            if not context.remaining: break
            for hit in provider.search(query,lang,limit):
                task=context.candidate(hit.url,text=hit.title+' '+hit.snippet,name=hit.name,website=hit.website,via='search:'+provider.name,low_prior=bool(hit.extra.get('low_prior')))
                if task: tasks.append(task)
    return tasks
