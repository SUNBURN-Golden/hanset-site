"""Bounded sitemap/feed/pagination expansion without cursor loops."""
import re
from lxml.etree import XMLSyntaxError
from urllib.parse import urljoin, urlsplit, unquote
from ..extract import sitemap, feed, html

def expand(context,url, *, depth=0, parent_url=None, max_children=20, max_candidates=60, slug_include=(), slug_exclude=(), child_include=(), via="sitemap", _seen=None, _children=None):
    if depth>2: return []
    seen=_seen if _seen is not None else set()
    children=_children if _children is not None else [0]
    if max_candidates<=0: return []
    if url in seen: return []
    seen.add(url)
    result=context.get(url,kind='sitemap')
    if not result or not result.ok: return []
    tasks=[]
    try:
        doc=sitemap.extract(result.content,url)
    except (ValueError,TypeError,XMLSyntaxError): return []
    if doc['kind']=='index':
        for child in doc['urls']:
            if child_include and not any(re.search(term,unquote(child),re.I) for term in child_include): continue  # category restriction
            if children[0]>=min(20,max_children): break
            children[0]+=1
            tasks.extend(expand(context,child,depth=depth+1,parent_url=url,max_children=max_children,max_candidates=max_candidates-len(tasks),slug_include=slug_include,slug_exclude=slug_exclude,child_include=child_include,via=via,_seen=seen,_children=children))
            if len(tasks)>=max_candidates: break
    else:
        for entry in doc['entries']:
            link=entry['url']
            path=unquote(urlsplit(link).path)
            if slug_include and not any(re.search(term,path,re.I) for term in slug_include): continue
            if any(re.search(term,path,re.I) for term in slug_exclude): continue
            task=context.candidate(link,depth=0,via=via,parent_url=url,low_prior=True,domain_limit=max_candidates)
            if task:
                tasks.append(task)
                if entry.get('lastmod'):
                    from datetime import datetime
                    from ..util.timeutil import kst_now
                    from ..scheduler import score
                    try:
                        stamp=datetime.fromisoformat(entry['lastmod'].replace('Z','+00:00'))
                        now=kst_now(context.clock)
                        if stamp.tzinfo is None: stamp=stamp.replace(tzinfo=now.tzinfo)
                        urgency=1/(1+max(0,(now-stamp).total_seconds()/86400))
                        with context.db:
                            context.db.execute('UPDATE tasks SET urgency=?,priority=coalesce(priority,0)+? WHERE task_id=?',(urgency,score({'urgency':urgency}),task))
                    except ValueError: pass
            if len(tasks)>=max_candidates: break
    return tasks


def advertised_sitemaps(text):
    return re.findall(r'^\s*Sitemap:\s*(\S+)', text, re.I | re.M)

def robots_sitemaps(context, origin):
    result = context.get(urljoin(origin, '/robots.txt'), kind='robots')
    roots = [urljoin(origin, '/sitemap.xml')]
    if result and result.ok:
        roots.extend(urljoin(origin, url) for url in advertised_sitemaps(result.text)[:20])
    tasks = []
    for url in dict.fromkeys(roots):
        tasks.extend(expand(context, url, parent_url=origin))
    return tasks

def expand_feed(context,url):
    result=context.get(url,kind='feed')
    if not result or not result.ok: return []
    tasks=[]
    for item in feed.extract(result.content,url)['items']:
        link=item.get('url') or item.get('link')
        if link:
            task=context.candidate(urljoin(url,link),text=str(item),via='feed',parent_url=url)
            if task: tasks.append(task)
    return tasks

def list_pages(context,url, *, max_pages=8, url_filter='', low_prior=False):
    tasks=[];queue=[url];seen=set();hashes=set()
    while queue and len(seen)<min(8,max_pages):
        current=queue.pop(0)
        if current in seen: continue
        seen.add(current)
        result=context.get(current)
        if not result or not result.ok: continue
        if result.content_hash in hashes and result.content_hash: break
        if result.content_hash: hashes.add(result.content_hash)
        doc=html.extract(result.content,current)
        for link in doc['links']:
            target=link['url']
            if link['kind']=='next_page' or re.search(r'[?&]page=\d+|/page/\d+',target) or link['text'].isdigit():
                if urlsplit(target).netloc==urlsplit(current).netloc and target not in seen: queue.append(target)
                continue
            if url_filter and not re.search(url_filter,target): continue
            task=context.candidate(target,text=link['text'],via='directory:html_list',parent_url=current,low_prior=low_prior)
            if task: tasks.append(task)
    return tasks
