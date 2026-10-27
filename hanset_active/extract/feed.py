"""RSS, Atom and JSON API documents (no remote entity expansion)."""
import json
from lxml import etree
from ..util.pii import strip_pii
NAME='feed'
VERSION='1.0.0'

def extract(content: str | bytes, url: str = '') -> dict:
    content=strip_pii(content)
    text=content.decode() if isinstance(content,bytes) else content
    if text.lstrip().startswith(('{','[')):
        data=json.loads(text)
        return dict(extractor=NAME,extractor_version=VERSION,items=data if isinstance(data,list) else data.get('items',data.get('results',[data])),text=text)
    root=etree.fromstring(text.encode(),etree.XMLParser(resolve_entities=False,no_network=True))
    items=[]
    for node in root.xpath('//*[local-name()="item" or local-name()="entry"]'):
        def value(name):
            found=node.xpath('./*[local-name()=$n]',n=name)
            return ''.join(found[0].itertext()).strip() if found else ''
        link=node.xpath('./*[local-name()="link"]')
        items.append(dict(title=value('title'),url=(link[0].get('href') or value('link')) if link else '',text=value('description') or value('summary') or value('content'),date=value('pubDate') or value('updated') or value('published')))
    return dict(extractor=NAME,extractor_version=VERSION,items=items,text='\n'.join(i['text'] for i in items))
