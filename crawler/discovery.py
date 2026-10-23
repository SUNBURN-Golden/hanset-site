"""Bounded URL discovery. Parsing never authorizes fetching discovered destinations."""
import json
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urljoin
from .config import Rejected
from .privacy_filter import safe_url, text
from .schema import digest


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            value = dict(attrs).get('href')
            if value and len(self.links) < 500:
                self.links.append(value)


def discover(kind, raw, base):
    if len(raw) > 2000000:
        raise Rejected('discovery_too_large')
    urls = []
    if kind in ('rss', 'atom', 'sitemap'):
        if '<!DOCTYPE' in raw.upper() or '<!ENTITY' in raw.upper():
            raise Rejected('unsafe_xml')
        try: root = ET.fromstring(raw)
        except ET.ParseError: raise Rejected('invalid_feed') from None
        for e in root.iter():
            tag = e.tag.rsplit('}', 1)[-1]
            if tag == 'loc' or tag == 'link':
                if e.attrib.get('rel', 'alternate') in ('alternate', 'canonical'):
                    urls.append(e.attrib.get('href') or e.text or '')
    elif kind == 'links':
        parser = Links()
        parser.feed(raw)
        urls = parser.links
    else:
        try: data = json.loads(raw)
        except ValueError: raise Rejected('invalid_api') from None
        if kind == 'crossref':
            urls = [x.get('URL', '') for x in data.get('message', {}).get('items', [])]
        elif kind == 'pubmed':
            result = data.get('result', {})
            ids = result.get('uids', data.get('esearchresult', {}).get('idlist', []))
            urls = ['https://pubmed.ncbi.nlm.nih.gov/' + str(x) + '/' for x in ids if str(x).isdigit()]
        elif kind == 'search':
            urls = [x.get('url', '') for x in data.get('results', [])]
        else:
            raise Rejected('unknown_adapter')
    out = []
    for u in urls[:500]:
        try: u = safe_url(urljoin(base, u))
        except Rejected: continue
        if u not in out:
            out.append(u)
    return out


def ingest(worker, kind, raw, base, depth=0, cursor=None):
    """Called only with memory-resident response from the gated fetcher."""
    if depth > worker.cfg['max_depth']:
        raise Rejected('depth_limit')
    previous = worker.store.db.execute('SELECT data FROM checkpoints WHERE seed=?', (safe_url(base),)).fetchone()
    checkpoint = json.loads(previous[0]) if previous else {'cursors': [], 'urls': []}
    cursor_key = digest(cursor) if cursor else None
    if cursor_key and cursor_key in checkpoint['cursors']:
        raise Rejected('repeated_cursor')
    urls = discover(kind, raw, base)
    complete = True
    for url in urls:
        if url not in checkpoint['urls']:
            if len(checkpoint['urls']) >= 500:
                complete = False
                break
            try:
                worker.candidate(url, depth=depth)
            except Rejected as exc:
                if str(exc) in ('quota_exhausted', 'defer_until_next_day'):
                    complete = False
                    break  # Keep progress and accept the page already collected.
                continue  # One excluded/unsafe destination cannot revoke its parent.
            checkpoint['urls'].append(url)
    if cursor_key and complete:
        checkpoint['cursors'].append(cursor_key)
    if len(checkpoint['cursors']) > 20 or len(checkpoint['urls']) > 500:
        raise Rejected('pagination_limit')
    worker.store.db.execute('INSERT OR REPLACE INTO checkpoints VALUES (?,?)', (safe_url(base), json.dumps(checkpoint)))
    return urls
