"""Public directory adapters using the common guarded discovery budget."""
from urllib.parse import urlencode
from .search import Nominatim
from .sitemaps import expand, expand_feed, list_pages

def discover(context, directory):
    if not directory.get('enabled',True): return []
    kind=directory['kind'];url=directory['url']
    if kind=='sitemap': return expand(context,url,max_candidates=directory.get('max_candidates',12))
    if kind=='rss': return expand_feed(context,url)
    if kind=='html_list': return list_pages(context,url,max_pages=directory.get('max_pages',8),url_filter=directory.get('url_filter',''))
    if kind!='osm_search': raise ValueError('Unknown directory kind')
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
