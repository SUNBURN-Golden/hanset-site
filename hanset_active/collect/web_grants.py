"""Track which current input still grants a fetched page its evidence binding."""
from collections import defaultdict
import json

from hanset_active.util.urls import registrable_domain

from .targets import fetch_url_allowed

SCHEMA = '''
CREATE TABLE IF NOT EXISTS web_search_grants (
    scope TEXT NOT NULL, grant_id TEXT NOT NULL, urls_json TEXT NOT NULL,
    PRIMARY KEY (scope, grant_id)
);
CREATE TABLE IF NOT EXISTS web_receipt_scopes (
    scope TEXT NOT NULL, receipt_id TEXT NOT NULL REFERENCES receipts(receipt_id),
    parent_url TEXT, binding_current INTEGER NOT NULL,
    PRIMARY KEY (scope, receipt_id)
);
CREATE INDEX IF NOT EXISTS web_receipt_scopes_by_receipt ON web_receipt_scopes(receipt_id, binding_current);
'''


def record_receipt_scope(db, scope, receipt_id, parent_url):
    db.execute('INSERT OR REPLACE INTO web_receipt_scopes VALUES (?,?,?,1)', (scope, receipt_id, parent_url))


class WebGrants:
    def __init__(self, db, scope, *, complete, disabled, testing):
        self.db, self.scope, self.testing = db, scope, testing
        self.complete, self.disabled = complete, disabled
        self.searches = set()

    def search_urls(self, grant, urls):
        self.searches.add(grant)
        if urls is None:
            row = self.db.execute('SELECT urls_json FROM web_search_grants WHERE scope=? AND grant_id=?',
                                  (self.scope, grant)).fetchone()
            return json.loads(row[0]) if row else []
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO web_search_grants VALUES (?,?,?)',
                            (self.scope, grant, json.dumps(urls)))
        return urls

    def reconcile(self, jobs):
        roots = defaultdict(list)
        for job in jobs:
            roots[(job['requested_url'], job['venue_id'], job['venue_key'])].append(job)
        retired = []
        rows = self.db.execute('''SELECT r.*, s.parent_url FROM web_receipt_scopes s
            JOIN receipts r USING(receipt_id) WHERE s.scope=? AND s.binding_current=1''', (self.scope,))
        for receipt in rows:
            venue = receipt['venue_id'], receipt['venue_key']
            if not self.complete and venue not in self.disabled:
                continue
            source = receipt['parent_url'] or receipt['requested_url']
            candidates = roots.get((source, *venue), ())
            if not any(self._matches(receipt, job) for job in candidates):
                retired.append(receipt['receipt_id'])
        with self.db:
            if self.complete:
                saved = self.db.execute('SELECT grant_id FROM web_search_grants WHERE scope=?', (self.scope,))
                expired = set(row[0] for row in saved) - self.searches
                self.db.executemany('DELETE FROM web_search_grants WHERE scope=? AND grant_id=?',
                                    ((self.scope, grant) for grant in expired))
            self.db.executemany('UPDATE web_receipt_scopes SET binding_current=0 WHERE scope=? AND receipt_id=?',
                                ((self.scope, receipt_id) for receipt_id in retired))
            self.db.executemany('''UPDATE receipts SET binding_current=0 WHERE receipt_id=? AND NOT EXISTS (
                SELECT 1 FROM web_receipt_scopes WHERE receipt_id=? AND binding_current=1)''',
                                ((receipt_id, receipt_id) for receipt_id in retired))

    def _matches(self, receipt, job):
        final = receipt['final_url']
        scope = job['scope_domains'] & {registrable_domain(job['url'])}
        if not fetch_url_allowed(final, scope, testing=self.testing):
            return False
        auth = job['source_authorization']
        if receipt['parent_url']:
            return bool(auth and fetch_url_allowed(receipt['requested_url'], scope, testing=self.testing))
        stored = json.loads(receipt['authorization_json']) if receipt['authorization_json'] else None
        return stored == ({**auth, 'final_url': final} if auth else None)

