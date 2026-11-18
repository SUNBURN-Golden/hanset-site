"""closed_month.json summary on the closed-days guide: renders while the month is current, past dates drop, block hides after month end."""
import datetime as dt
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'site'))
import build_site as B

SLUGS = ('novotel-ambassador-seoul-gangnam', 'banyan-tree-club-seoul', 'fraser-place-central-seoul',
         'ambassador-seoul-pullman', 'orakai-cheonggyesan', 'choice-by-vantt-gangdong', 'park-hyatt-seoul')
MUTED = '<p class="muted">Hours can change, so check before you go.</p>'
_BUILT = {}


def build_for(today):
    """Render the site in-process for a build date (what main() does with --today); cached per date."""
    if today not in _BUILT:
        venues, events, *_ = B.load(today)
        site = B.Site(today, True)
        B.build(site, venues, events, today)
        _BUILT[today] = (site.pages['/guides/closed-days-korea/'], {v['slug']: v for v in venues})
    return _BUILT[today]


def block_of(page):
    start = page.index('<section class="closed-month"')
    return page[start:page.index('</section>', start) + len('</section>')]


def anchor(slug, by_slug, suffix=''):
    c = B.CLOSED_ALL[slug]
    return f'<a href="{B.E(c["url"])}" rel="noopener noreferrer">{B.E(by_slug[slug]["title"])}</a>{suffix}'


class ClosedMonthSummary(unittest.TestCase):
    def test_october_build_renders_six_dates_with_official_urls(self):
        page, by_slug = build_for(dt.date(2026, 10, 29))
        block = block_of(page)
        self.assertIn('<h2>November closed days</h2>', block)
        self.assertEqual(block.count('<li>'), 6)
        for date_txt in ('Nov 2 (Mon)', 'Nov 3 (Tue)', 'Nov 4 (Wed)', 'Nov 9 (Mon)', 'Nov 11 (Wed)', 'Nov 12 (Thu)'):
            self.assertIn(f'<li>{date_txt}:', block)
        for slug in SLUGS:
            self.assertIn(anchor(slug, by_slug), block)
        self.assertIn(anchor('fraser-place-central-seoul', by_slug, ' (may change around holidays)'), block)
        self.assertIn(anchor('park-hyatt-seoul', by_slug, ' (maintenance)'), block)
        self.assertTrue(block.endswith(MUTED + '</section>'))
        # sits at the top of the page: right after the lead, before the confirmed-days table
        self.assertLess(page.index('<h1>'), page.index('<p class="lead">'))
        self.assertLess(page.index('<p class="lead">'), page.index('<section class="closed-month"'))
        self.assertLess(block.index('</section>'), page.index('<table class="facts">'))
        self.assertLess(page.index('<section class="closed-month"'), page.index('Only places we’ve confirmed'))

    def test_november_build_drops_past_dates_keeps_rest(self):
        page, by_slug = build_for(dt.date(2026, 11, 5))
        block = block_of(page)
        for gone in ('Nov 2 (Mon)', 'Nov 3 (Tue)', 'Nov 4 (Wed)'):
            self.assertNotIn(gone, block)
        self.assertNotIn(by_slug['novotel-ambassador-seoul-gangnam']['title'], block)
        for slug in ('orakai-cheonggyesan', 'choice-by-vantt-gangdong', 'park-hyatt-seoul'):
            self.assertIn(anchor(slug, by_slug, ' (maintenance)' if slug == 'park-hyatt-seoul' else ''), block)
        self.assertIn(MUTED, block)

    def test_december_build_hides_the_block(self):
        page, _ = build_for(dt.date(2026, 12, 1))
        self.assertNotIn('closed-month', page)
        self.assertNotIn('November closed days', page)
        self.assertNotIn('Nov 12 (Thu)', page)
        self.assertIn('Only places we’ve confirmed', page)  # guide itself still renders


if __name__ == '__main__':
    unittest.main()


def test_build_dates_do_not_leak_active_closure_state(monkeypatch):
    """Build later/earlier/later with the same evidence and no global rebinding."""
    today = dt.date(2032, 1, 5)
    venues, events, *_ = B.load(today)
    v = venues[0]
    checked = today - dt.timedelta(days=91)
    monkeypatch.setattr(B, 'CLOSED_ALL', {v['slug']: dict(
        closed='Temporary maintenance', checked=checked.isoformat(), source='Official notice',
        url='https://example.org/closure', until=today.isoformat())})
    monkeypatch.setattr(B, 'CLOSED_MONTH', dict(month='2032-01', title='January closed days', items=[
        dict(date=today.isoformat(), slugs=[v['slug']]),
        dict(date='2032-01-12', slugs=[v['slug']])]))
    for build_date in (today + dt.timedelta(days=1), today, dt.date(2032, 2, 1), today):
        site = B.Site(build_date, True)
        B.build(site, venues, events, build_date)
        detail = site.pages[f'/finder/{v["slug"]}/']
        closed = detail.split('<th>Closed on</th>', 1)[1].split('</tr>', 1)[0]
        guide = site.pages['/guides/closed-days-korea/']
        table = guide.split('<tbody>', 1)[1].split('</tbody>', 1)[0]
        active = build_date <= today
        assert ('Official notice' in closed) == active
        assert ('Official notice' in table) == active
        assert ('Check before you go' in closed) != active
        if active:
            assert B.dot(checked) in closed and B.OUTDATED in closed
        if build_date.month == 1:
            block = block_of(guide)
            # Monthly summaries retain their own occurrence/month visibility rules,
            # even after a date-bound detail/table record has expired.
            assert ('Jan 5 (Mon)' in block) == active
            assert 'Jan 12 (Mon)' in block
            assert B.dot(checked) in block and 'Official notice' in block
            assert B.OUTDATED in block
        else:
            assert 'closed-month' not in guide
