"""Memory-only HTTP extraction; no full-body disk cache."""
from html.parser import HTMLParser
import re
from .config import Rejected, policy_for
from .network_gate import NetworkGate
from .privacy_filter import text


class MinimalHTML(HTMLParser):
    """First semantic candidate only; HTTP visibility checks are HTML/inline-only."""
    BLOCKED = {'script', 'style', 'title', 'input', 'textarea', 'iframe', 'form', 'nav', 'footer', 'header', 'noscript', 'svg', 'template'}
    VOID = {'input', 'meta', 'link', 'img', 'br', 'hr', 'source', 'area', 'wbr', 'embed', 'param', 'col', 'base'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.parts, self.title_parts = [], [], []
        self.links = []
        self.locator = None
        self.candidate_seen = False
        self.region_eligible = False
        self.region_depth = None
        self.region_closed = False

    @staticmethod
    def hidden_style(style):
        # HTTP cannot resolve stylesheets or layout. Fail closed for inline hiding,
        # including duplicate declarations, regardless of their cascade order.
        style = re.sub(r'/\*.*?\*/', '', style, flags=re.S)
        for declaration in style.split(';'):
            key, _, value = declaration.partition(':')
            key = key.strip().lower()
            value = re.sub(r'\s+', '', value).lower().removesuffix('!important')
            if (key == 'display' and value == 'none' or
                    key == 'visibility' and value in ('hidden', 'collapse')):
                return True
        return False

    def handle_starttag(self, tag, attrs):
        a = {key: value or '' for key, value in attrs}
        cls_id = (a.get('class', '') + ' ' + a.get('id', '')).lower()
        blocked = (tag in self.BLOCKED or 'hidden' in a or 'contenteditable' in a
                   or a.get('aria-hidden', '').strip().lower() == 'true'
                   or self.hidden_style(a.get('style', '')))
        blocked |= any(x in cls_id for x in ('private', 'profile', 'message', 'comment', 'chat'))
        inherited = self.stack[-1][1] if self.stack else False
        if tag in ('main', 'article') and not self.candidate_seen:
            self.candidate_seen = True
            self.region_eligible = not (inherited or blocked)
            if self.region_eligible:
                self.locator = tag
                self.region_depth = len(self.stack)
        selected = self.region_depth is not None and not self.region_closed
        if selected and not (inherited or blocked) and tag == 'a' and a.get('href'):
            self.links.append(a['href'])
        if tag not in self.VOID:
            self.stack.append((tag, blocked or inherited, selected))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                if self.stack[i][2] and not self.stack[i][1] and tag in ('p', 'div', 'li', 'section', 'article', 'main', 'tr'):
                    self.parts.append('\n')
                if self.region_depth is not None and i <= self.region_depth:
                    self.region_closed = True
                    self.region_depth = None
                del self.stack[i:]
                break

    def handle_data(self, data):
        if (self.stack and self.stack[-1][0] == 'title'
                and not any(frame[1] for frame in self.stack[:-1])):
            self.title_parts.append(data)
        if self.stack and self.stack[-1][2] and not self.stack[-1][1]:
            self.parts.append(data)

    def result(self, contacts=()):
        if not self.candidate_seen:
            raise Rejected('missing_minimal_region')
        if not self.region_eligible:
            raise Rejected('private_region')
        raw_text = ''.join(self.parts).strip()
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
            parser.close()
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
