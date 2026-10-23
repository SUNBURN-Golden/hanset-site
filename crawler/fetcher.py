"""Memory-only HTTP extraction; no full-body disk cache."""
from html.parser import HTMLParser
from .config import Rejected, policy_for
from .network_gate import NetworkGate
from .privacy_filter import text


class MinimalHTML(HTMLParser):
    BLOCKED = {'script', 'style', 'input', 'textarea', 'iframe', 'form', 'nav', 'footer', 'header', 'noscript', 'svg', 'template'}
    VOID = {'input', 'meta', 'link', 'img', 'br', 'hr', 'source', 'area', 'wbr', 'embed', 'param', 'col', 'base'}

    REGION_DIVS = ('container', 'content', 'contents', 'wrapper', 'wrap', 'detail', 'root', 'sub_con', 'facility')

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.parts, self.title_parts, self.meta_parts = [], [], [], []
        self.links = []
        self.locator = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'meta' and (a.get('name', '').lower() in ('description', 'og:description') or a.get('property', '').lower() in ('og:description', 'og:title')) and a.get('content'):
            self.meta_parts.append(a['content'])
        cls_id = (a.get('class', '') + ' ' + a.get('id', '')).lower()
        blocked = tag in self.BLOCKED or 'hidden' in a or 'contenteditable' in a or a.get('aria-hidden') == 'true' or 'display:none' in a.get('style', '').replace(' ', '').lower()
        blocked |= any(x in cls_id for x in ('private', 'profile', 'message', 'comment', 'chat'))
        inherited = self.stack[-1][1] if self.stack else False
        is_region = tag in ('main', 'article') or (not (inherited or blocked) and (tag == 'section' or (tag == 'div' and any(k in cls_id for k in self.REGION_DIVS))))
        selected = (self.stack[-1][2] if self.stack else False) or is_region
        if is_region and not self.locator:
            self.locator = tag
        if selected and not (inherited or blocked) and tag == 'a' and a.get('href'):
            self.links.append(a['href'])
        if tag not in self.VOID:
            self.stack.append((tag, blocked or inherited, selected))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        if tag in ('p', 'div', 'li', 'section', 'article', 'main', 'tr'):
            self.parts.append('\n')

    def handle_data(self, data):
        if self.stack and self.stack[-1][0] == 'title':
            self.title_parts.append(data)
        if self.stack and self.stack[-1][2] and not self.stack[-1][1]:
            self.parts.append(data)

    def result(self, contacts=()):
        if not self.locator:
            raise Rejected('missing_minimal_region')
        raw_text = ''.join(self.parts).strip() or ' '.join(self.meta_parts + self.title_parts).strip()
        excerpt = text(raw_text, contacts)[:2000]
        if not excerpt:
            raise Rejected('empty_region')
        return {'title': text(''.join(self.title_parts), contacts)[:240], 'excerpt': excerpt,
                'locator': self.locator, 'facts': [], 'published_at': None}


def collect(worker, reservation):
    gate = NetworkGate(worker, reservation)
    try:
        if reservation['channel'] == 'browser_extract':
            from .browser_runner import BrowserRunner
            data = BrowserRunner(worker, reservation, gate).run()
            return worker.accept(reservation, data)
        response = gate.fetch(reservation['url'])
        policy = policy_for(worker.cfg, response['url'], now=worker.clock())
        import json
        record = worker.store.db.execute('SELECT data FROM records WHERE id=?', (reservation['record_id'],)).fetchone()
        depth = json.loads(record[0])['depth']
        archive = None
        kind = policy.get('adapter')
        raw = response['body'].decode('utf-8', 'replace')
        if kind:
            from .discovery import ingest
            if depth < worker.cfg['max_depth']:
                ingest(worker, kind, raw, response['url'], depth=depth + 1)
            return worker.accept_discovery(reservation, response['url'], response['redirects'])
        else:
            parser = MinimalHTML()
            parser.feed(raw)
            data = parser.result(worker.cfg['public_contacts'])
            if policy.get('archive_enabled') and response['url'] in policy.get('full_text_permissions', {}):
                archive = text(''.join(parser.parts), worker.cfg['public_contacts'])
            if policy.get('follow_links') and depth < worker.cfg['max_depth']:
                from .discovery import ingest
                from html import escape
                links = ''.join('<a href="' + escape(u, quote=True) + '"></a>' for u in parser.links[:500])
                ingest(worker, 'links', links, response['url'], depth + 1)
        data['document_key'] = reservation['document_id']
        data['final_url'] = response['url']
        data['redirects'] = response['redirects']
        return worker.accept(reservation, data, archive=archive)
    except BaseException:
        worker.revoke(reservation)
        raise
    finally:
        gate.cancel()
