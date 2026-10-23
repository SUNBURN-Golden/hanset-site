"""Fixed loopback intake; never an HTTP proxy. Extension origins are explicit policy."""
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit
from .config import Rejected, policy_for

MAX_BODY = 64000


def handler(worker):
    class Intake(BaseHTTPRequestHandler):
        server_version = 'HANSET-Local'
        def setup(self):
            super().setup()
            self.connection.settimeout(5)
        def log_message(self, *_):
            pass  # No URLs, headers or payloads in access logs.

        def allowed(self):
            origin = self.headers.get('Origin')
            return (origin in worker.cfg['extension_origins'] and
                    self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}' and
                    self.path in ('/reserve', '/begin', '/result', '/status', '/revoke', '/grant', '/submit', '/policies', '/consent'))

        def reply(self, status, value):
            encoded = json.dumps(value).encode()
            self.send_response(status)
            if self.headers.get('Origin') in worker.cfg['extension_origins']:
                self.send_header('Access-Control-Allow-Origin', self.headers['Origin'])
                self.send_header('Vary', 'Origin')
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_OPTIONS(self):
            if not self.allowed():
                self.reply(403, {'reason': 'origin_or_endpoint_denied'})
                return
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', self.headers['Origin'])
            self.send_header('Access-Control-Allow-Methods', 'POST')
            self.send_header('Access-Control-Allow-Headers', 'content-type')
            self.end_headers()

        def do_POST(self):
            if not self.allowed() or self.headers.get('Content-Type') != 'application/json' or self.headers.get('Transfer-Encoding'):
                self.reply(403, {'reason': 'intake_denied'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_BODY:
                    raise Rejected('request_size')
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict):
                    raise Rejected('invalid_payload')
                if self.path == '/reserve':
                    result = worker.reserve(value['clip_id'], value['url'], value['document_id'], 'manual_clip')
                elif self.path == '/policies':
                    result = {'origins': [p['origin'] for p in worker.cfg['policies'] if p.get('browser')]}
                elif self.path == '/consent':
                    from .consent import exchange
                    result = exchange(worker, self.headers['Origin'], value.get('answers', []))
                elif self.path == '/grant':
                    origin = value['origin']
                    policy_for(worker.cfg, origin + '/', browser=True, now=worker.clock())
                    enabled = value.get('enabled') is True
                    worker.store.db.execute('INSERT OR REPLACE INTO grants VALUES (?,?,?)', (origin, self.headers['Origin'], int(enabled)))
                    if not enabled:
                        for row in worker.store.db.execute("SELECT * FROM reservations WHERE channel='browser_extract' AND state IN ('reserved','running')").fetchall():
                            if row['url'].startswith(origin + '/'):
                                worker.revoke(dict(row))
                    result = {'enabled': enabled}
                elif self.path == '/submit':
                    url = value['url']
                    origin = 'https://' + urlsplit(url).netloc
                    grant = worker.store.db.execute('SELECT enabled FROM grants WHERE origin=? AND extension_origin=?', (origin, self.headers['Origin'])).fetchone()
                    if not grant or not grant[0]:
                        raise Rejected('permission_not_granted')
                    policy_for(worker.cfg, url, browser=True, now=worker.clock())
                    rid = worker.candidate(url, channel='browser_extract')
                    with worker.store.transaction() as db:
                        record = json.loads(db.execute('SELECT data FROM records WHERE id=?', (rid,)).fetchone()[0])
                        record['extension_origin'] = self.headers['Origin']
                        record['channel'] = 'browser_extract'
                        record['recheck_requested'] = True
                        worker._save(db, record)
                    result = {'record_id': rid}
                else:
                    res = value['reservation']
                    stored = worker.status(res)
                    if stored['channel'] != 'manual_clip':
                        raise Rejected('internal_job_only')
                    if self.path == '/begin':
                        result = worker.begin(res)
                    elif self.path == '/result':
                        result = worker.accept(res, value['data'])
                    elif self.path == '/status':
                        result = stored
                    else:
                        result = {'drained': worker.revoke(res)}
                self.reply(200, result)
            except Rejected as e:
                self.reply(409, {'reason': str(e)})
            except (KeyError, ValueError, TypeError):
                self.reply(400, {'reason': 'invalid_request'})
    return Intake


def serve(worker, bind='127.0.0.1', port=8766):
    if bind != '127.0.0.1':
        raise Rejected('loopback_only')
    server = HTTPServer((bind, port), handler(worker))
    server.timeout = 1
    worker.supervisor.recover()
    try:
        while True:
            server.handle_request()
            worker.supervisor.expire()
    finally:
        server.server_close()
