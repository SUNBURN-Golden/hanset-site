"""url_include on a seed's approved policy keeps broad feeds on-topic (crawler.discovery.ingest)."""
import datetime as dt
import sqlite3
import types
import unittest

from crawler.discovery import ingest

SITEMAP = ('<urlset>'
           '<url><loc>https://feed.test/event/hyrox-seoul-2026</loc></url>'
           '<url><loc>https://feed.test/event/hyrox-london-2026</loc></url>'
           '<url><loc>https://feed.test/event/hyrox-busan-2026</loc></url>'
           '</urlset>')


def worker(url_include):
    now = dt.datetime.fromisoformat('2026-01-01T12:00:00+09:00').timestamp()
    policy = {'origin': 'https://feed.test', 'public': True, 'paths': ['/'], 'disallow': [],
              'robots_reviewed_at': '2026-01-01', 'crawl_delay': 5.0}
    if url_include is not None:
        policy['url_include'] = url_include
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE checkpoints(seed TEXT PRIMARY KEY, data TEXT)')
    seen = []
    return types.SimpleNamespace(cfg={'max_depth': 2, 'policies': [policy]},
                                 store=types.SimpleNamespace(db=db),
                                 clock=lambda: now,
                                 candidate=lambda url, depth=0: seen.append(url)), seen


class UrlIncludeTest(unittest.TestCase):
    def test_filter_applies(self):
        w, seen = worker(['/event/[^/]*(seoul|busan)'])
        ingest(w, 'sitemap', SITEMAP, 'https://feed.test/event-sitemap.xml')
        self.assertEqual(sorted(seen), ['https://feed.test/event/hyrox-busan-2026',
                                        'https://feed.test/event/hyrox-seoul-2026'])

    def test_no_filter_keeps_all(self):
        w, seen = worker(None)
        ingest(w, 'sitemap', SITEMAP, 'https://feed.test/event-sitemap.xml')
        self.assertCountEqual(seen, ['https://feed.test/event/hyrox-seoul-2026',
                                     'https://feed.test/event/hyrox-london-2026',
                                     'https://feed.test/event/hyrox-busan-2026'])


if __name__ == '__main__':
    unittest.main()
