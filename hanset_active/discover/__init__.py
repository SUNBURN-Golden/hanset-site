"""Bounded discovery context shared by all six discovery paths.

HTTP getters must implement Fetcher.fetch(url, kind=...). Inject a bound Fetcher
method (including for fixtures) so robots, rate limits and daily caps apply.
"""
from collections import Counter
from ..config import load_profile
from ..scheduler import enqueue
from ..util.urls import canonicalize, is_denied, is_excluded, registrable_domain
from ..venues import upsert_venue

class Discovery:
    def __init__(self, fetcher, *, getter=None, budget=100, per_domain=12, threshold=.25):
        self.fetcher=fetcher;self.db=fetcher.db;self.clock=fetcher.clock
        self.getter=getter or fetcher.fetch
        self.remaining=max(0,budget);self.per_domain=per_domain;self.threshold=threshold
        self.counts=Counter();self.candidate_counts=Counter();self.seen=set();self.profile=fetcher.settings.profile
    def relevance(self,text):
        text=text.casefold()
        type_match=any(w.casefold() in text for w in self.profile['relevance_keywords'])
        region_match=any(w.casefold() in text for r in self.profile['regions'] for w in r['aliases'])
        return min(1,.6*type_match+.4*region_match)
    def get(self,url, *, kind='body', **kwargs):
        try: url=canonicalize(url)
        except ValueError: return None
        domain=registrable_domain(url)
        if not self.remaining or (kind!='sitemap' and self.counts[domain]>=self.per_domain) or is_denied(url) or is_excluded(url): return None
        self.remaining-=1;self.counts[domain]+=1
        return self.getter(url,kind=kind,**kwargs)
    def candidate(self,url, *, text='', name='', city='', region='', website='', depth=0, via='search', parent_url=None, parent_task_id=None, low_prior=False, domain_limit=None):
        if depth>2 or depth<0 or is_excluded(url,text,name,website): return None
        try: url=canonicalize(url)
        except ValueError: return None
        if domain_limit is None and self.candidate_counts[registrable_domain(url)]>=self.per_domain: return None
        if is_denied(url) or url in self.seen or (not low_prior and self.relevance(text+' '+name+' '+url)<self.threshold): return None
        if not self.fetcher.register_candidate(url): return None
        self.seen.add(url)
        self.candidate_counts[registrable_domain(url)]+=1
        task=self.db.execute('SELECT task_id FROM tasks WHERE dedupe_key=?',(url,)).fetchone()[0]
        provenance=via+(':'+parent_url if parent_url else '')
        with self.db:
            self.db.execute('UPDATE tasks SET kind=?,depth=?,parent_url=?,parent_task_id=?,discovered_via=?,payload=? WHERE task_id=?',('trial',depth,parent_url,parent_task_id,provenance,'{}',task))
            if via.startswith(('sitemap','directory')):  # directory leads are the discovery backbone: ahead of re-trials of already-seeded sites
                self.db.execute('UPDATE tasks SET priority=0.9 WHERE task_id=?',(task,))
        if name and website:
            upsert_venue(self.db,name,website=website,city=city,region=region,discovered_via=provenance,clock=self.clock)
        return task
    def search_task(self,name, *, region='', type_term='sauna', depth=1, via='mentions', parent_url=None):
        if not self.remaining or depth>2 or is_excluded(name): return None
        return enqueue(self.db,kind='search',payload={'query':f'"{name}" {type_term} {region}'},depth=depth,discovered_via=via,parent_url=parent_url,clock=self.clock,novelty=1)
