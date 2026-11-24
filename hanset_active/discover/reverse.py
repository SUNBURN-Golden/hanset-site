"""Ordered gap strategies translated to idempotent backfill tasks."""
import re
from ..scheduler import enqueue
from ..config import load_profile
from ..util.followups import allows_followup, portal_domains
STRATEGIES=('site_links','lang_counterpart','attachments','images','search','directory')

def followups(db,venue,field,documents=(), *, strategy,clock=None,depth=0):
    if strategy not in STRATEGIES: raise ValueError('Unknown gap strategy')
    if depth>2: return []
    config=next(f for f in load_profile()['fields'] if f['id']==field)
    keywords=config['keywords_ko']+config['keywords_en']
    tasks=[]; portals=portal_domains()
    if strategy in ('search','directory'):
        payload={'query':f'"{venue["name"]}" {keywords[0]}','field':field,'strategy':strategy}
        task=enqueue(db,kind=strategy,payload=payload,venue_id=venue['venue_id'],task_class='backfill',discovered_via='reverse:'+strategy,clock=clock,gap_importance=config['importance'])
        return [task] if task else []
    for document in documents:
        for link in document.get('links',[]):
            if not allows_followup(venue,link['url'],strategy,portals): continue
            text=(link['url']+' '+link.get('text','')).casefold();kind=link['kind']
            match=(strategy=='site_links' and kind in ('internal','external') and (field in link.get('fields', ()) or any(k.casefold() in text for k in keywords))) or (strategy=='lang_counterpart' and (link.get('hreflang') or re.search(r'/en/|/ko/',text))) or (strategy=='attachments' and kind=='attachment') or (strategy=='images' and kind=='image_notice')
            if match:
                task=enqueue(db,kind='ocr' if strategy=='images' else 'fetch',url=link['url'],depth=depth+1,payload={'field':field,'strategy':strategy},venue_id=venue['venue_id'],task_class='backfill',discovered_via='reverse:'+strategy,clock=clock,gap_importance=config['importance'])
                if task: tasks.append(task)
    return tasks
