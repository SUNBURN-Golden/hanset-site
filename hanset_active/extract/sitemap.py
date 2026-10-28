from lxml import etree
from ..util.pii import strip_pii
NAME='sitemap'
VERSION='1.0.0'

def extract(content: str | bytes, url: str = '') -> dict:
    data=strip_pii(content)
    root=etree.fromstring(data.encode() if isinstance(data,str) else data, etree.XMLParser(resolve_entities=False,no_network=True))
    kind='index' if etree.QName(root).localname=='sitemapindex' else 'urlset'
    entries=[]
    for node in root:
        values={etree.QName(c).localname:(c.text or '').strip() for c in node if isinstance(c.tag,str)}
        if values.get('loc'):
            entries.append(dict(url=values['loc'],lastmod=values.get('lastmod')))
    return dict(extractor=NAME,extractor_version=VERSION,kind=kind,entries=entries,urls=[e['url'] for e in entries])
