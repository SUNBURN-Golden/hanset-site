"""Public directory adapters using the common guarded discovery budget."""
from urllib.parse import urlencode
from .search import Nominatim, usable_providers
from .sitemaps import expand, expand_feed, list_pages

def discover(context, directory):
    if not directory.get('enabled',True): return []
    kind=directory['kind'];url=directory['url']
    if kind=='sitemap': return expand(context,url,max_candidates=directory.get('max_candidates',60),slug_include=directory.get('slug_include',()),slug_exclude=directory.get('slug_exclude',()),via='directory:'+directory.get('id','sitemap'))
    if kind=='rss': return expand_feed(context,url)
    if kind=='html_list': return list_pages(context,url,max_pages=directory.get('max_pages',8),url_filter=directory.get('url_filter',''))
    if kind!='osm_search': raise ValueError('Unknown directory kind')
    if not usable_providers(context.db,clock=context.clock,names=['nominatim']): return []
    provider=Nominatim(context.get,context.db,clock=context.clock,endpoint=url)
    tasks=[]
    for region in context.profile['regions']:
        for venue_type in context.profile['venue_types']:
            for page in range(min(8,directory.get('max_pages',8)) if directory.get('paginate') else 1):
                if not context.remaining: return tasks
                query=f'{venue_type["en_terms"][0]} {region["osm_city"]}'
                params=provider.params(query,'en',10);params['offset']=page*10
                result=context.get(url+'?'+urlencode(params),kind='api')
                if not result or not result.ok:
                    provider.state('inaccessible',result.reason if result else 'budget exhausted');break
                hits=provider.parse(result,10)
                if not hits: break
                added=0
                for hit in hits:
                    task=context.candidate(hit.url,text=hit.title+' '+query,name=hit.name,website=hit.website,city=region['osm_city'],region=region['id'],via='directory:osm_search')
                    if task: tasks.append(task);added+=1
                if not added: break
    return tasks


def detail(content, url, profile):
    """Parse an explicitly configured directory detail; return sanitized identity."""
    import re
    from bs4 import BeautifulSoup
    from urllib.parse import urlsplit, urljoin
    from ..extract import html
    from ..util.pii import strip_pii
    from ..util.urls import canonicalize, is_denied, is_excluded
    directory=next((d for d in profile.get('directories',[]) if d.get('directory') and
                    ((urlsplit(url).hostname or '')==urlsplit(d['url']).hostname or (urlsplit(url).hostname or '').endswith('.'+(urlsplit(d['url']).hostname or '')))),None)
    if not directory: return None
    soup=BeautifulSoup(strip_pii(content),'lxml')
    node=soup.select_one('meta[property="og:title"]')
    title=node.get('content','') if node else ''
    if not title:
        node=soup.select_one('h1') or soup.title
        title=node.get_text(' ',strip=True) if node else ''
    name=re.split(r'\s*[|｜]\s*|\s+[-–]\s+',title)[0].strip()
    doc=html.extract(content,url)
    external=[]
    for link in doc['links']:
        if link['kind']!='external' or is_denied(link['url']) or is_excluded(link['url'],link['text']): continue
        if urlsplit(link['url']).hostname==urlsplit(url).hostname: continue
        external.append(link)
    labelled=set()
    for anchor in soup.select('a[href]'):
        parent=anchor.find_parent(['tr','dd','p','li']) or anchor
        if re.search(r'홈페이지|website|homepage',parent.get_text(' ',strip=True),re.I):
            try: labelled.add(canonicalize(urljoin(url,anchor['href'])))
            except ValueError: pass
    external.sort(key=lambda link:0 if canonicalize(link['url']) in labelled else 1)
    website=canonicalize(external[0]['url']) if external else ''
    address=''
    for node in soup.select('tr,dl,p,address,li'):
        text=node.get_text(' ',strip=True)
        match=re.search(r'(?:주소|Address)\s*[:：|]?\s*(.{3,180})',text,re.I)
        if match:
            address=strip_pii(match[1]);break
        if node.name=='address':
            address=strip_pii(text)[:180];break
    return dict(name=strip_pii(name),website=website,address=address,directory_id=directory['id'],low_prior=directory.get('low_prior',False),document=doc)
