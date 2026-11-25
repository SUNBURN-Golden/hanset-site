"""Only host-side outbound path. DNS-pinned TLS, no proxies/cookies/automatic redirects."""
import email.utils
import http.client
import ipaddress
import socket
import ssl
import threading
import time
import uuid
import re
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit, urljoin
from hanset_active.crawl_policy import KoreanNewsRobots, is_korean_news
from hanset_active.locks import host_lock
from .config import Rejected, policy_for
from .leases import current
from .privacy_filter import safe_url, text

USER_AGENT = 'HANSET-Collector/1.0 (+mailto:hanset@soulbound.co.kr)'
ROBOTS_TTL = 86400
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
ROBOTS_DIRECTIVE = re.compile(r'^\s*(?:user-agent|allow|disallow|crawl-delay)\s*:', re.I)

ALLOWED_TYPES = ('text/html', 'application/xhtml+xml', 'application/json', 'application/xml',
                 'text/xml', 'application/rss+xml', 'application/atom+xml', 'text/plain',
                 'application/javascript', 'text/javascript', 'text/css')
TRANSITION_NETWORKS = tuple(ipaddress.ip_network(n) for n in
                            ('64:ff9b::/96', '2002::/16', '2001::/32'))


class BlockScreen(HTMLParser):
    """Visible challenge instructions, not embedded contact-form CAPTCHA scripts."""
    def __init__(self):
        super().__init__()
        self.hidden = []
        self.parts = []
        self.password_input = False

    def handle_starttag(self, tag, attrs):
        if tag == 'input' and (dict(attrs).get('type') or '').casefold() == 'password':
            self.password_input = True
        if tag in ('script', 'style', 'template', 'noscript', 'form', 'iframe'):
            self.hidden.append(tag)

    def handle_endtag(self, tag):
        if self.hidden and self.hidden[-1] == tag:
            self.hidden.pop()

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def blocked(self):
        visible = ' '.join(' '.join(self.parts).lower().split())
        return self.password_input or bool(re.search(
            r'access denied|sign in to continue|login required|paywall|subscribers only|'
            r'subscription (?:is )?required|subscribe to read|로그인이 필요|접근이 차단|'
            r'verify (?:that )?you are (?:a )?human|complete (?:the |this )?captcha|'
            r'checking your browser|보안 문자.{0,20}입력|로봇이 아님.{0,20}확인', visible)
            or visible.strip() == 'captcha')


def public_addresses(host, resolver=socket.getaddrinfo):
    result = []
    for info in resolver(host, 443, type=socket.SOCK_STREAM):
        raw = info[4][0]
        address = ipaddress.ip_address(raw)
        effective = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address
        if (not effective.is_global or effective.is_multicast or effective.is_reserved or
                any(address in net for net in TRANSITION_NETWORKS)):
            raise Rejected('non_public_destination')
        result.append(raw)
    if not result:
        raise Rejected('dns_empty')
    return sorted(set(result))


class PinnedTLS(http.client.HTTPSConnection):
    def __init__(self, host, ip, timeout):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self.ip = ip
        self.transport = None

    def connect(self):
        raw = socket.create_connection((self.ip, 443), self.timeout)
        self.transport = raw
        try:
            if raw.getpeername()[0] != self.ip:
                raise Rejected('peer_mismatch')
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            self.transport = self.sock
        except BaseException:
            raw.close()
            raise


class NetworkGate:
    def __init__(self, worker, reservation):
        self.worker, self.reservation = worker, reservation
        self.connections = set()
        self.lock = threading.Lock()
        self.cancelled = False
        self.backed_off = set()
        worker.supervisor.gates[reservation['id']] = self

    def cancel(self):
        with self.lock:
            self.cancelled = True
            for conn in tuple(self.connections):
                transport = conn.transport or conn.sock
                if transport:
                    try: transport.shutdown(socket.SHUT_RDWR)
                    except OSError: pass
                    transport.close()
                conn.close()

    def _retry_after(self, value, now, fallback):
        try: return now + max(0, int(value))
        except (TypeError, ValueError):
            try: return max(now, email.utils.parsedate_to_datetime(value).timestamp())
            except (TypeError, ValueError, OverflowError): return now + fallback

    def request(self, url, method='GET', resource_type='document', browser=False):
        if self.cancelled or method not in ('GET', 'HEAD') or resource_type not in ('document', 'script', 'stylesheet', 'fetch', 'xhr'):
            raise Rejected('request_not_allowed')
        w = self.worker
        url = safe_url(url)
        policy = policy_for(w.cfg, url, browser, now=w.clock())
        delay = 0
        if is_korean_news(url):
            delay = max(5, w.cfg['min_interval'], float(policy.get('crawl_delay', 0)))
            try:
                delay = max(delay, self._robots(url, browser, delay))
            except Rejected as exc:
                w.store.db.execute('INSERT INTO audit(operation,reason,at) VALUES (?,?,?)',
                                   ('news_robots', str(exc), w.clock()))
                raise
        return self._dispatch(url, method, browser, delay)

    def _robots(self, url, browser, delay):
        w = self.worker
        host = urlsplit(url).hostname
        origin = 'https://' + urlsplit(url).netloc
        target = origin + '/robots.txt'
        with host_lock(w.store.path.parent, host, robots=True):
            current(w.store.db, self.reservation, w.clock(), running=True)
            cached = w.store.db.execute('SELECT body FROM news_robots WHERE origin=? AND expires_at>?',
                                        (origin, w.clock())).fetchone()
            if cached:
                parser = KoreanNewsRobots(cached['body'], user_agent=USER_AGENT)
            else:
                visited = set()
                for _ in range(w.cfg['max_redirects'] + 1):
                    visited.add(target)
                    result = self._dispatch(target, 'GET', browser, delay, robots_for=url)
                    if result['status'] not in REDIRECT_STATUSES:
                        break
                    target = result['headers']['location']
                    parts = urlsplit(target)
                    if (target in visited or f'{parts.scheme}://{parts.netloc}' != origin
                            or parts.path != '/robots.txt'):
                        raise Rejected('robots_unavailable')
                if result['status'] not in (200, 404, 410):
                    raise Rejected('robots_unavailable')
                decoded = result['body'].decode('utf-8', 'replace')
                if re.match(r'^\s*(?:access denied|just a moment|captcha|verify you are human|login required)', decoded, re.I):
                    w.store.db.execute('UPDATE domains SET stopped_until=? WHERE host=?',
                                       (w.clock() + ROBOTS_TTL, host))
                    raise Rejected('blocked_page')
                body = ''
                if result['status'] == 200:
                    if decoded.lstrip().startswith('<'):
                        raise Rejected('robots_unavailable')
                    body = '\n'.join(line.split('#', 1)[0].strip() for line in decoded.splitlines()
                                     if ROBOTS_DIRECTIVE.match(line))
                    try:
                        safe = text(unquote(body)) == unquote(body)
                    except Rejected:
                        safe = False
                    if not safe:
                        body = 'User-agent: *\nDisallow: /'
                parser = KoreanNewsRobots(body, user_agent=USER_AGENT)
                now = w.clock()
                with w.store.transaction() as db:
                    db.execute('INSERT OR REPLACE INTO news_robots VALUES (?,?,?,?,?)',
                               (origin, body, parser.delay, now, now + ROBOTS_TTL))
                    # Apply a newly learned crawl delay to the robots dispatch too.
                    if parser.delay > delay:
                        db.execute('UPDATE domains SET next_at=next_at+? WHERE host=?',
                                   (parser.delay - delay, host))
            if not parser.can_fetch(url):
                raise Rejected('robots_disallowed')
            return parser.delay

    def _dispatch(self, url, method, browser, delay, *, robots_for=None):
        w, res = self.worker, self.reservation
        # Only _robots can use the path exemption, for the authorized origin.
        authorization_url = robots_for or url
        policy_for(w.cfg, authorization_url, browser, now=w.clock())
        extension_origin = res.get('extension_origin')
        origin = 'https://' + urlsplit(res['url']).netloc
        if extension_origin and not w.browser_grant_enabled(w.store.db, origin, extension_origin):
            raise Rejected('permission_revoked')
        host = urlsplit(url).hostname
        domain = w.store.db.execute('SELECT next_at,stopped_until FROM domains WHERE host=?', (host,)).fetchone()
        if host in self.backed_off or (domain and domain['stopped_until'] > w.clock()):
            raise Rejected('domain_held')
        # Waiting never holds a SQLite transaction. Recheck authority afterwards.
        if delay and domain and domain['next_at'] > w.clock():
            wait = domain['next_at'] - w.clock()
            if w.clock() + wait >= res['expires']:
                raise Rejected('lease_too_short')
            time.sleep(wait)
        if extension_origin:
            from .consent import require
            require(w, res, 'https://' + urlsplit(url).netloc)
        ips = public_addresses(host)
        request_id, conn, response = str(uuid.uuid4()), None, None
        with w.store.transaction() as db:
            row = current(db, res, w.clock(), running=True)
            if extension_origin and not w.browser_grant_enabled(db, origin, extension_origin):
                raise Rejected('permission_revoked')
            policy_for(w.cfg, authorization_url, browser, now=w.clock())
            if row['request_count'] >= w.cfg['max_requests']:
                raise Rejected('request_budget_exhausted')
            db.execute('INSERT OR IGNORE INTO domains(host) VALUES (?)', (host,))
            domain = db.execute('SELECT * FROM domains WHERE host=?', (host,)).fetchone()
            if (host in self.backed_off or domain['stopped_until'] > w.clock()
                    or (delay and domain['next_at'] > w.clock()) or domain['owner'] not in (None, res['id'])):
                raise Rejected('domain_held')
            if db.execute("SELECT 1 FROM requests WHERE host=? AND state='active'", (host,)).fetchone():
                raise Rejected('domain_busy')
            # Subresources/redirects also occupy their domain until the entire
            # generation is drained. Finishing one socket cannot free a lease.
            if domain['owner'] is None:
                db.execute('UPDATE domains SET owner=?,epoch=epoch+1 WHERE host=?', (res['id'], host))
            db.execute('INSERT INTO requests VALUES (?,?,?,?,?,?)', (request_id, res['id'], res['generation'], host, 'active', w.clock()))
            db.execute('UPDATE domains SET next_at=? WHERE host=?', (w.clock() + delay, host))
            db.execute('UPDATE reservations SET request_count=request_count+1 WHERE id=?', (res['id'],))
        try:
            conn = PinnedTLS(host, ips[0], min(w.cfg['request_timeout'], max(0.1, res['expires'] - w.clock())))
            with self.lock:
                if self.cancelled:
                    raise Rejected('lease_revoked')
                self.connections.add(conn)
            current(w.store.db, res, w.clock(), running=True)
            u = urlsplit(url)
            conn.request(method, u.path + ('?' + u.query if u.query else ''), headers={
                'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity', 'Accept': '*/*'})
            response = conn.getresponse()
            status = response.status
            now = w.clock()
            if status in (401, 403, 407, 429) or status >= 500 or (robots_for and status in (202, 402, 451)):
                until = now + (86400 if status < 500 else 300)
                if status in (429, 503):
                    self.backed_off.add(host)
                    until = self._retry_after(response.getheader('Retry-After'), now, 3600 if status == 429 else 300)
                failures = w.store.db.execute('SELECT failures FROM domains WHERE host=?', (host,)).fetchone()[0]
                if status >= 500 and failures >= 2:
                    until = 1e20  # operator intervention after three server failures
                w.store.db.execute('UPDATE domains SET stopped_until=?,failures=failures+1 WHERE host=?', (until, host))
                raise Rejected('server_stop')
            if response.getheader('Content-Encoding', 'identity') != 'identity':
                raise Rejected('compressed_response_not_allowed')
            content_type = response.getheader('Content-Type', '')
            ctype = content_type.split(';')[0].strip().lower()
            headers = {}
            if status in REDIRECT_STATUSES:
                try:
                    headers['location'] = safe_url(urljoin(url, response.getheader('Location', '')))
                except Rejected:
                    if robots_for:
                        raise Rejected('robots_unavailable') from None
                    raise
                if not robots_for:
                    policy_for(w.cfg, headers['location'], browser, now=w.clock())
                    # No destination socket has been opened yet.
                    public_addresses(urlsplit(headers['location']).hostname)
                body = b''
            elif robots_for and status in (404, 410) and not ctype:
                body = b''
            else:
                if ctype not in ALLOWED_TYPES:
                    raise Rejected('media_not_allowed')
                # Bound the total response deadline, including slow trickles.
                chunks, size = [], 0
                deadline = min(res['expires'], w.clock() + w.cfg['request_timeout'])
                while not response.isclosed():
                    current(w.store.db, res, w.clock(), running=True)
                    remaining = deadline - w.clock()
                    if remaining <= 0:
                        raise Rejected('response_deadline')
                    if conn.transport:
                        conn.transport.settimeout(remaining)
                    chunk = response.read1(min(65536, w.cfg['max_response_bytes'] + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > w.cfg['max_response_bytes']:
                        raise Rejected('response_too_large')
                body = b''.join(chunks)
                if len(body) > w.cfg['max_response_bytes']:
                    raise Rejected('response_too_large')
                if ctype in ('text/html', 'application/xhtml+xml'):
                    sample = body.decode('utf-8', 'replace').lower()
                    screen = BlockScreen()
                    screen.feed(sample)
                    if screen.blocked():
                        w.store.db.execute('UPDATE domains SET stopped_until=? WHERE host=?', (now + 86400, host))
                        raise Rejected('blocked_page')
                headers['content-type'] = content_type  # Preserve charset for rendered Korean text/URLs.
            with w.store.transaction() as db:
                row = current(db, res, w.clock(), running=True)
                if row['byte_count'] + len(body) > w.cfg['max_total_bytes']:
                    raise Rejected('total_bytes_exhausted')
                db.execute('UPDATE reservations SET byte_count=byte_count+? WHERE id=?', (len(body), res['id']))
                if 200 <= status < 400:
                    db.execute('UPDATE domains SET failures=0 WHERE host=?', (host,))
            return {'url': url, 'status': status, 'headers': headers, 'body': body}
        except ssl.SSLCertVerificationError:
            raise Rejected('tls_certificate_failure') from None
        except OSError as exc:
            raise Rejected('network_failure_' + str(exc.errno)) from None
        except http.client.HTTPException:
            raise Rejected('http_protocol_failure') from None
        except UnicodeError:
            raise Rejected('url_encoding_failure') from None
        finally:
            if conn:
                if response:
                    response.close()
                if conn.transport:
                    conn.transport.close()
                conn.close()
                with self.lock:
                    self.connections.discard(conn)
            w.store.db.execute("UPDATE requests SET state='closed' WHERE id=?", (request_id,))

    def fetch(self, url):
        history = []
        for _ in range(self.worker.cfg['max_redirects'] + 1):
            response = self.request(url)
            if response['status'] in REDIRECT_STATUSES:
                history.append({'url': url, 'status': response['status']})
                url = response['headers']['location']
                continue
            if response['status'] != 200:
                raise Rejected('http_status')
            response['redirects'] = history
            return response
        raise Rejected('redirect_limit')
