"""closed_month.json summary on the closed-days guide: renders while the month is current, past dates drop, block hides after month end."""
import datetime as dt
from pathlib import Path
import sys
import unittest

import pytest

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
            block = block_of(guide)
            assert 'Jan 5 (Mon)' in block and 'Jan 12 (Mon)' not in block
            for markup in (closed, table, block):
                assert B.dot(checked) in markup and 'Official notice' in markup
                assert B.OUTDATED in markup
        else:
            assert 'closed-month' not in guide


@pytest.mark.parametrize('invalid', [
    {'source': 'Venue reply'}, {'source': 'Operator-run profile'}, {'source': 'Third-party notice'},
    {'url': '/closure'}, {'checked': None}, {'checked': ''}, {'checked': '11/2'}, {'checked': '2026-02-30'},
    {'checked': '2026-11-01 or 2026-11-02'}, {'checked': '2026-11-02'},
])
def test_unqualified_closure_never_enters_detail_guide_or_month(monkeypatch, invalid):
    today = dt.date(2026, 11, 1)
    venues, events, *_ = B.load(today)
    v = venues[0]
    v['facts'].append(('Closed', 'Closed on the fixture maintenance day'))
    c = dict(closed='Fixture maintenance', source='Official notice',
             url='https://example.org/closure', checked=today.isoformat()) | invalid
    monkeypatch.setattr(B, 'CLOSED_ALL', {v['slug']: c})
    monkeypatch.setattr(B, 'CLOSED_MONTH', dict(month='2026-11', title='November closed days',
                        items=[dict(date='2026-11-02', slugs=[v['slug']])]))
    site = B.Site(today, True)
    B.build(site, [v], events, today)
    guide = site.pages['/guides/closed-days-korea/']
    assert 'closed-month' not in guide and 'Fixture maintenance' not in guide
    detail = site.pages[f'/finder/{v["slug"]}/']
    assert '<th>Closed on</th><td class="unk">Check before you go</td>' in detail
    assert '<th>Closed</th><td>Not confirmed yet</td>' in detail
    assert 'Fixture maintenance' not in detail


def test_month_filters_each_venue_and_drops_empty_dates(monkeypatch):
    today = dt.date(2026, 11, 1)
    venues, events, *_ = B.load(today)
    by_slug = {v['slug']: v for v in venues}
    valid, invalid = SLUGS[:2]
    closures = {slug: B.CLOSED_ALL[slug].copy() for slug in (valid, invalid)}
    closures[invalid]['checked'] = '2026-11-02'
    monkeypatch.setattr(B, 'CLOSED_ALL', closures)
    monkeypatch.setattr(B, 'CLOSED_MONTH', dict(month='2026-11', title='November closed days', items=[
        dict(date='2026-11-02', slugs=[valid, invalid]),
        dict(date='2026-11-03', slugs=[invalid]),
    ]))
    site = B.Site(today, True)
    B.build(site, venues, events, today)
    block = block_of(site.pages['/guides/closed-days-korea/'])
    assert block.count('<li>') == 1 and 'Nov 2 (Mon)' in block
    assert anchor(valid, by_slug) in block and anchor(invalid, by_slug) not in block
    assert 'Nov 3 (Tue)' not in block


def test_month_uses_the_same_sunday_precedence_as_detail_and_table(monkeypatch):
    today = dt.date(2026, 11, 1)
    explicit, shim, legacy_open = ('crossfit-super-sapiens', 'crossfit-one-daeyeon', SLUGS[0])
    closures = {slug: dict(closed=B.SUNDAY_CLOSURE, source='Official page', checked='2026-10-18',
                           url=f'https://example.org/{slug}') for slug in (explicit, shim, legacy_open)}
    monkeypatch.setattr(B, 'CLOSED_ALL', closures)
    monkeypatch.setattr(B, 'CLOSED_MONTH', dict(month='2026-11', title='November closed days', items=[
        dict(date='2026-11-08', slugs=[explicit, shim, legacy_open]),
    ]))
    venues, events, *_ = B.load(today)
    by_slug = {v['slug']: v for v in venues}
    v = by_slug[explicit]
    v['facts'] = v['facts_ko'] = [('Hours', 'Weekdays 07:00–20:00')]
    v['c']['sunday_evidence'] = dict(status='yes', source_kind='official_page',
                                   source_url='https://example.org/sunday', checked_on='2026-10-18')
    v['sunday'], v['sunday_evidence'] = B.resolve_sunday(v, today)
    assert v['sunday'] == 'yes' and by_slug[shim]['sunday'] == 'no'
    by_slug[legacy_open]['sunday'] = 'yes'

    site = B.Site(today, True)
    B.build(site, venues, events, today)
    guide = site.pages['/guides/closed-days-korea/']
    for slug in closures:
        detail = site.pages[f'/finder/{slug}/']
        row = detail.split('<th>Closed on</th>', 1)[1].split('</tr>', 1)[0]
        url = closures[slug]['url']
        assert (url in row) == (slug == shim)
        assert (url in guide) == (slug == shim)
        assert (anchor(slug, by_slug) in block_of(guide)) == (slug == shim)


@pytest.mark.parametrize('expiry,active', [({}, True), ({'until': None}, True), ({'until': ''}, True),
                                         ({'until': '   '}, False), ({'until': '2026-02-30'}, False)])
def test_open_ended_closures_keep_their_age_across_surfaces(monkeypatch, expiry, active):
    today = dt.date(2026, 11, 1)
    venues, events, *_ = B.load(today)
    v = venues[0]
    slug = v['slug']
    record = dict(closed='Fixture maintenance', source='Official closure notice',
                  url='https://example.org/closure') | expiry
    monkeypatch.setattr(B, 'CLOSED_ALL', {slug: record})
    monkeypatch.setattr(B, 'CLOSED_MONTH', dict(month='2026-11', title='November closed days', items=[
        dict(date='2026-11-08', slugs=[slug]),
    ]))
    for age in (90, 91):
        checked = today - dt.timedelta(days=age)
        record['checked'] = checked.isoformat()
        site = B.Site(today, True)
        B.build(site, [v], events, today)
        guide = site.pages['/guides/closed-days-korea/']
        row = site.pages[f'/finder/{slug}/'].split('<th>Closed on</th>', 1)[1].split('</tr>', 1)[0]
        table = guide.split('<tbody>', 1)[1].split('</tbody>', 1)[0]
        assert ('closed-month' in guide) == active
        for markup in (row, table, block_of(guide) if active else ''):
            assert (record['url'] in markup and B.dot(checked) in markup) == active
            assert (B.OUTDATED in markup) == (active and age == 91)
