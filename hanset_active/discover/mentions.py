"""Conservative entity leads from structured data and bilingual names."""
import re
from ..util.pii import strip_pii
from ..util.urls import is_excluded
from ..venues import normalize_name

def names(document, *, public_source=False):
    text=strip_pii(document.get('text',''))
    found=[m.get('name','') for m in document.get('mentions',[])]
    for pattern in [r'[가-힣A-Za-z0-9]+(?:\s+[가-힣A-Za-z0-9]+)?(?:사우나|스파|찜질방|크로스핏|박스|스파앤)', r'\b(?:[A-Z][a-zA-Z]+\s+){1,3}(?:Sauna|Spa|Jjimjilbang|Bathhouse|CrossFit|HYROX)\b']:
        found.extend(m[0] for m in re.finditer(pattern,text))
    found.extend(re.findall(r'["“]([^"”]{2,60}(?:Sauna|Spa|사우나|스파))["”]',text))
    return list({normalize_name(n):n.strip() for n in found if isinstance(n, str) and n and (public_source or not is_excluded(n))}.values())

def discover(context,document, *, parent_url='',depth=0,region=''):
    known={normalize_name(r[0]) for r in context.db.execute('SELECT name FROM venues UNION SELECT name FROM seed_venues')}
    tasks=[]
    for name in names(document):
        if normalize_name(name) in known: continue
        if context.relevance(name+' '+region)<context.threshold: continue
        task=context.search_task(name,region=region,depth=depth+1,via='mentions',parent_url=parent_url)
        if task: tasks.append(task)
    return tasks
