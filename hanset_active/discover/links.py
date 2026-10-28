"""Record SNS/excluded links; follow useful official links at bounded depth."""
import re
from ..db import deterministic_id
from ..util.urls import is_excluded, registrable_domain, canonicalize
from ..util.pii import strip_pii

def discover(context,document, *, parent_url,venue_id=None,depth=0,source_state='registered'):
    tasks=[]
    for link in document.get('links',[]):
        url=strip_pii(link['url']);kind=link['kind']
        if is_excluded(url,link.get('text','')): continue
        if kind in ('sns','excluded_link','booking'):
            if venue_id:
                with context.db:
                    context.db.execute('INSERT OR IGNORE INTO venue_links VALUES (?,?,?,?,?)',(deterministic_id(venue_id,url,kind),venue_id,url,kind,'links:'+parent_url))
            if kind!='booking': continue
        if source_state!='registered': continue
        useful=kind in ('attachment','image_notice','booking','sitemap','next_page') or link.get('hreflang') or re.search(r'price|hours|rules|faq|요금|이용안내|규정|/en/|/ko/',url+' '+link.get('text',''),re.I)
        if useful:
            task=context.candidate(url,text=link.get('text',''),depth=depth+1,via='links',parent_url=parent_url,low_prior=True)
            if task: tasks.append(task)
    return tasks
