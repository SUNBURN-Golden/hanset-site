"""Explorer regressions. Node uses a DOM double; no browser is launched here."""
import datetime as dt
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'site'))
import build_site as B
from test_detail_evidence import sunday_fixture_cases, sunday_fixture_pages

ROOT = Path(__file__).resolve().parents[1]


class Elements(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.elements = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class Explorer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.today = dt.date(2026, 10, 23)
        cls.venues, events, *_ = B.load(cls.today)
        cls.by_slug = {v['slug']: v for v in cls.venues}
        cls.site = B.Site(cls.today, True)
        B.build(cls.site, cls.venues, events, cls.today)

    def test_minimal_home_and_finder_explorer_order(self):
        home, finder = (self.site.pages[p] for p in ('/', '/finder/'))
        self.assertNotIn('<form class="filters"', home)
        self.assertNotIn('class="illrow', home)
        self.assertIn('HANSET, Sweat on Korea', home)
        self.assertIn('/assets/js/search.js?v=', home)
        positions = [finder.index(token) for token in (
            '<form class="filters"', 'id="f-count"', 'id="finder-list"')]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('/assets/js/finder.js?v=', finder)
        self.assertIn('id="pictures"', self.site.pages['/guide/'])
        self.assertNotIn('/ads/', self.site.pages)

    def test_advanced_filters_closed_and_basic_fields_outside_details(self):
        page = self.site.pages['/finder/']
        form = page[page.index('<form class="filters"'):page.index('</form>')]
        basic, more = form.split('<details class="more-filters">')
        self.assertIn('<summary>More filters <span id="f-more-count"></span></summary>', more)
        self.assertNotIn(' open', more.split('>')[0])
        for field in ('f-sub', 'f-sort', 'f-sunday', 'f-price', 'f-hyrox', 'f-cf', 'f-reset'):
            self.assertIn(f'id="{field}"', more)
            self.assertNotIn(f'id="{field}"', basic)
        for token in ('data-seg="city"', 'data-seg="type"', 'id="f-q"'):
            self.assertIn(token, basic)

    def test_no_map_markup_or_view_toggle_on_any_page(self):
        for path, page in self.site.pages.items():
            with self.subTest(path=path):
                for token in ('venue-map', 'district-map', 'data-map-city', 'data-pin=',
                              'map-legend', 'map-selection', 'map-preview', 'map-heading',
                              'map-view', 'map-tabs', 'map-zone', 'selected-venue',
                              'Explore the map', 'Select a pin', 'Schematic map',
                              'Map/List', 'data-view=', 'view-toggle', '<template id="preview-'):
                    self.assertNotIn(token, page)

    def test_all_regional_venues_remain_in_the_list_once(self):
        for path in ('/finder/',):
            elements = Elements(self.site.pages[path]).elements
            cards = [a for tag, a in elements if tag == 'article' and 'data-slug' in a]
            self.assertEqual(len(cards), len(self.venues))
            self.assertEqual({a['data-slug'] for a in cards}, set(self.by_slug))
            self.assertTrue(all('hidden' not in a for a in cards))
            for city in ('seoul', 'busan', 'jeju', 'gyeonggi'):
                self.assertEqual({a['data-slug'] for a in cards if a['data-city'] == city},
                                 {v['slug'] for v in self.venues if v['city_key'] == city})

    def test_city_filter_is_single_control_without_map_tabs(self):
        for path in ('/finder/',):
            page = self.site.pages[path]
            self.assertEqual(page.count('data-seg="city"'), 1)
            group = re.search(r'<div class="seg" data-seg="city".*?</div>', page)[0]
            self.assertEqual([a['data-v'] for tag, a in Elements(group).elements if tag == 'button'],
                             ['', 'seoul', 'busan', 'jeju', 'gyeonggi'])

    def test_css_removes_map_styles_and_compacts_mobile_hero(self):
        css = (ROOT / 'site/static/css/site.css').read_text()
        for token in ('.map-', '#map-', '.venue-map', '.district-map', '.legend-pin',
                      '.pin-', '.river-label', '.selected-venue'):
            self.assertNotIn(token, css)
        self.assertIn('[hidden]{display:none!important}', css)
        self.assertIn('.home-search h1{font-size:40px;', css)
        self.assertIn('.search-form input{', css)
        self.assertNotIn('.home-hero', css)

    def test_finder_and_categories_keep_ordinary_venue_order(self):
        # Stable sorting keeps the original venue order when the other keys tie.
        ranked = sorted(self.venues, key=lambda v: (not v['price'], v['status'] != 'verified', -len(v['facts'])))
        for category in ('finder', 'sauna', 'crossfit', 'hyrox', 'gym'):
            with self.subTest(category=category):
                elements = Elements(self.site.pages[f'/{category}/']).elements
                actual = [a['data-slug'] for tag, a in elements if tag == 'article' and 'data-slug' in a]
                expected = [v['slug'] for v in ranked if category == 'finder' or category in B.venue_categories(v)]
                self.assertEqual(actual, expected)

    def test_details_keep_official_links_dates_and_freshness(self):
        for v in self.venues:
            with self.subTest(venue=v['slug']):
                page = self.site.pages[f'/finder/{v["slug"]}/']
                verify = page.split('<div class="verify">', 1)[1].split('</div>', 1)[0]
                sources = verify.split('<p>Source: ', 1)[1].split('</p>', 1)[0]
                source_links = [a for tag, a in Elements(sources).elements if tag == 'a']
                self.assertEqual([a['href'] for a in source_links], [v['source'], *(url for _, url in v['extra_sources'])])

                actions = page.split('<div class="btnrow detail-actions">', 1)[1].split('</div>', 1)[0]
                official = re.findall(r'<a\b[^>]*>Official site</a>', actions)
                official_links = [a for tag, a in Elements(''.join(official)).elements if tag == 'a']
                website = v['website']
                self.assertEqual([a['href'] for a in official_links], [website] if website and 'map.naver.com' not in website else [])
                for link in source_links + official_links:
                    self.assertEqual(set(link['rel'].split()), {'noopener', 'noreferrer'})
                self.assertIn(f'Last checked {v["last_verified"]:%Y.%m.%d}', verify)
                self.assertEqual('badge-old' in verify, (self.today - v['last_verified']).days > 90)

    def test_details_keep_original_next_venue_sequence(self):
        for v in self.venues:
            with self.subTest(venue=v['slug']):
                city = [entry for entry in self.venues if entry['city_key'] == v['city_key']]
                sequence = city if len(city) > 1 else self.venues
                next_venue = sequence[(sequence.index(v) + 1) % len(sequence)]
                page = self.site.pages[f'/finder/{v["slug"]}/']
                next_link = re.findall(r'<p class="next-venue"><a href="([^"]+)"', page)
                self.assertEqual(next_link, [f'/finder/{next_venue["slug"]}/'])

    def test_prices_keep_confirmed_terms_and_withhold_operator_profile_rates(self):
        for v in self.venues:
            for key, value in v['facts']:
                if B.is_price_context(key) and value.strip() and value != B.PENDING:
                    claim = B.resolve_publication(v, key, value, self.today)
                    if claim:
                        self.assertIn(B.E(claim.value), B.price_html(v, self.today), v['slug'])
            if v['price']:
                self.assertIn(B.E(v['price']), B.price_html(v, self.today))
                if v['price_caveat']:
                    self.assertIn(B.E(v['price_caveat']), B.price_html(v, self.today))
        repo = self.by_slug['solo-sauna-repo-noryangjin']
        terms = next(value for key, value in repo['facts'] if key == 'Price')
        page = self.site.pages['/finder/solo-sauna-repo-noryangjin/']
        self.assertNotIn(B.E(terms), page)
        self.assertNotIn('₩45,000', page)
        self.assertIn('<th>Price</th><td>Not confirmed yet</td>', page)
        # List cards are headline-only (PR #62): full terms live on the venue page.
        for output in (B.venue_card(repo, self.today), B.venue_contents(repo, self.today)):
            self.assertNotIn('class="price-short"', output)
            self.assertNotIn(B.E(terms), output)

    def test_access_age_time_tattoo_and_provenance_remain_verbatim(self):
        mandatory = {'Closed', 'Booking', 'Who can use', 'Conditions', 'Age', 'Capacity', 'Session length',
                     'Time allowed', 'Entry', 'Non-guests', 'Open-air bath rules', 'Types · length'}
        for v in self.venues:
            rows = [row for _, values in B.fact_groups(v) for row in values]
            _, sunday_claims = B.route_sunday_facts(v['facts'], v['facts_ko'])
            for key, value in v['facts']:
                if value.strip() and (key.startswith('Hours') or key in mandatory):
                    remainder, _, clause = value.rpartition(' · ')
                    if (key, clause) in sunday_claims and (key, value) not in sunday_claims:
                        self.assertIn((key, remainder), rows, v['slug'])
                    else:
                        self.assertIn((key, value), rows + sunday_claims, v['slug'])
            for claim in [row for row in rows if isinstance(row, B.SundayClaim)] + sunday_claims:
                rendered = B.fact_value_html(claim, v, self.today)
                evidence = v['sunday_evidence']
                if not evidence or evidence.source_url != v['source']:
                    self.assertNotIn(f'href="{B.E(v["source"])}"', rendered)
                if not evidence or evidence.checked_on != v['last_verified']:
                    self.assertNotIn(f'Checked {B.dot(v["last_verified"])}', rendered)
            self.assertTrue(any(key == 'Tattoos' for key, _ in rows))
            if not B.FOREIGNER.get(v['slug'], {}).get('tattoo') and not any('tattoo' in val.lower() for _, val in v['facts'] + [('Tip', tip) for tip in v['tips']]):
                self.assertIn(('Tattoos', B.UNKNOWN), rows)
        grand = self.by_slug['grand-josun-busan']
        self.assertIn(B.E("Children 12 and under can't enter, for safety."), self.site.pages['/finder/grand-josun-busan/'])
        sisu = self.by_slug['sisu-house']
        for key, value in sisu['facts']:
            if key in ('Booking', 'Capacity'):
                self.assertIn(B.E(value), B.price_html(sisu, self.today) + B.venue_facts(sisu, self.today))

    def test_zest_weekday_hours_survive_and_unverified_sunday_claims_stay_pending(self):
        zest = self.by_slug['crossfit-zest']
        page = self.site.pages['/finder/crossfit-zest/']
        main_hours = next(value for key, value in zest['facts'] if key == 'Hours (main box)')
        remainder, clause = main_hours.rsplit(' · ', 1)
        self.assertNotIn(B.E(main_hours), page)
        official = B.fact_provenance(zest, remainder, self.today)
        self.assertIn(f'href="{B.E(zest["source"])}"', official)
        self.assertEqual(page.count(B.E(remainder) + official), 2)
        self.assertNotIn(B.E(clause), page)
        hannam = self.by_slug['crossfit-hannam']
        reply = next(value for key, value in hannam['facts'] if key == 'Sunday')
        hannam_page = self.site.pages['/finder/crossfit-hannam/']
        self.assertNotIn(B.E(reply), hannam_page)
        self.assertIn('<th>Sunday</th><td>Not confirmed yet</td>', hannam_page)

    def test_pending_reply_is_unlinked_and_confirmed_booking_keeps_official_provenance(self):
        grand = self.by_slug['grand-hyatt-seoul']
        zest = self.by_slug['crossfit-zest']
        cases = (
            (grand, B.FOREIGNER[grand['slug']]['restriction'], 'awaiting official hotel reply', False),
            (zest, next(value for _, value in zest['facts'] if 'after an email reply' in value),
             'after an email reply', True),
        )
        for v, value, phrase, confirmed in cases:
            with self.subTest(venue=v['slug']):
                self.assertIn(phrase, value)
                note = B.fact_provenance(v, value, self.today)
                self.assertNotIn('Venue reply', note)
                self.assertIn(f'Checked {B.dot(v["last_verified"])}', note)
                if confirmed:
                    self.assertIn('Official source', note)
                    self.assertIn(f'href="{B.E(v["source"])}"', note)
                else:
                    self.assertIn('Unconfirmed', note)
                    self.assertNotIn('Official source', note)
                    self.assertNotIn('href=', note)
                self.assertIn(B.E(value) + note, self.site.pages[f'/finder/{v["slug"]}/'])

    def test_shared_official_and_reply_citation_does_not_use_closure_date(self):
        v = self.by_slug['paradise-hotel-busan']
        value = next(value for key, value in v['facts'] if key == 'Closed')
        self.assertIn('October: 10/28 (official page and hotel reply)', value)
        detail = self.site.pages[f'/finder/{v["slug"]}/']
        self.assertNotIn(B.E(value), detail)
        self.assertIn('<th>Closed</th><td>Not confirmed yet</td>', detail)

    def test_dash_reply_provenance_for_paradise_and_sisu_facts(self):
        for slug, checked, keys in (
                ('paradise-hotel-busan', '2026-10-17', {'Guest discount rate', 'Non-guests'}),
                ('sisu-house', '2026-10-18', {'Price', 'Hours', 'Closed', 'Booking', 'Capacity'})):
            v = self.by_slug[slug]
            facts = [(key, value) for key, value in v['facts'] if key in keys]
            self.assertEqual({key for key, _ in facts}, keys)
            outputs = (self.site.pages[f'/finder/{slug}/'],)  # cards/previews headline-only (PR #62)
            for key, value in facts:
                with self.subTest(venue=slug, fact=key):
                    self.assertIn(f'— venue reply, {checked}', value)
                    if key in ('Booking', 'Capacity'):
                        note = B.fact_provenance(v, value, self.today)
                        self.assertIn(f'Venue reply · {checked} ·', note)
                        self.assertNotIn('href=', note)
                        for output in outputs:
                            self.assertIn(B.E(value) + note, output)
                    else:
                        for output in outputs:
                            self.assertIn(f'<th>{key}</th><td>Not confirmed yet</td>', output)
                            self.assertNotIn(B.E(value), output)
                            for amount in re.findall(r'₩[\d,]+', value):
                                self.assertNotIn(amount, output)

    def test_reply_dates_are_scoped_to_explicit_citations(self):
        v = self.by_slug['paradise-hotel-busan']
        cases = (
            ('Closed 10/28 (venue reply 10/19)', '10/19'),
            ('Closed 10/28 (hotel reply 2026-10-18)', '2026-10-18'),
            ('Closed 10/28 (venue reply)', 'date not confirmed'),
            ('Closed 10/28 (hotel reply)', 'date not confirmed'),
            ('Closed 10/28 — venue reply, 2026-10-18', '2026-10-18'),
            ('Closed 10/28 — hotel reply, 2026.10.18', '2026.10.18'),
            ('Closed 10/28 -- venue reply, 2026-10-17', '2026-10-17'),
            ('Closed 10/28 -- hotel reply, 10/19', '10/19'),
            ('Closed 10/28 — venue reply', 'date not confirmed'),
            ('Closed 10/28 -- hotel reply', 'date not confirmed'),
            ('Closed 10/28 — venue reply (inquiry 10/20)', 'date not confirmed'),
            ('Closed 10/28 — venue reply\nInquiry 10/20', 'date not confirmed'),
        )
        for value, checked in cases:
            with self.subTest(value=value):
                note = B.fact_provenance(v, value, self.today)
                self.assertIn(f'Venue reply · {checked}', note)
                self.assertNotIn('10/28', note)
                self.assertNotIn('10/20', note)
                self.assertNotIn('Official source', note)
                self.assertNotIn('href=', note)
        for value in ('Closed 10/28 (awaiting official hotel reply)',
                      'Drop-in (after an email reply from staff)'):
            with self.subTest(value=value):
                note = B.fact_provenance(v, value, self.today)
                self.assertNotIn('Venue reply', note)
                self.assertIn(f'href="{B.E(v["source"])}"', note)

    def test_conflicting_access_is_unresolved_not_a_checklist_claim(self):
        v = self.by_slug['paradise-hotel-busan']
        self.assertTrue(B.access_conflict(v))
        rows = [row for _, values in B.fact_groups(v) for row in values]
        self.assertTrue(any(key == 'Visitor access' and value.startswith(B.UNKNOWN) for key, value in rows))
        for key, value in v['facts']:
            if key in ('Who can use', 'Non-guests'):
                self.assertIn((key, value), rows)
                self.assertIn(B.E(value) if key == 'Who can use' else 'Non-guests:</strong> Not confirmed yet',
                              B.venue_facts(v, self.today))
        self.assertNotIn('No. Hotel guests only.', B.venue_contents(v, self.today))

    def test_sauna_package_fields_reach_glance(self):
        packages = [v for v in self.venues if v['kind_key'] == 'sauna' and 'public_access' in B.FOREIGNER.get(v['slug'], {})]
        self.assertEqual(len(packages), 10)
        self.assertEqual(len(B.SAUNA_PKG_KEYS), 11)
        for v in packages:
            groups = dict(B.fact_groups(v))
            glance = self.site.pages[f'/finder/{v["slug"]}/'].split('id="glance-heading"', 1)[1].split('</section>', 1)[0]
            outputs = (glance,)  # list cards are headline-only since PR #62
            for key, label in B.SAUNA_PKG_KEYS:
                with self.subTest(venue=v['slug'], key=key):
                    value = B.FOREIGNER[v['slug']][key]
                    value = value if B._is_confirmed(value) else B.UNKNOWN
                    group = 'Hours & Closed' if key in ('last_entry', 'closures') else 'Visitor / Tattoo rules' if key in ('scrub_fee', 'age_tattoo_attire') else 'Access / Booking'
                    self.assertIn((label, value), groups[group])
                    pending = {
                        'sisu-house': {'full_price', 'last_entry', 'closures'},
                        'solo-sauna-repo-noryangjin': {'full_price', 'last_entry', 'closures'},
                    }
                    rendered = B.UNKNOWN if key in pending.get(v['slug'], set()) else value
                    for output in outputs:
                        self.assertIn(f'<strong>{B.E(label)}:</strong> {B.E(rendered)}', output)

    def test_closed_days_expire(self):
        import datetime as _dt
        before = B.active_closed(_dt.date(2026, 10, 28))
        self.assertIn('termeden-icheon', before); self.assertIn('park-hyatt-seoul', before)
        after = B.active_closed(_dt.date(2026, 10, 29))
        self.assertNotIn('termeden-icheon', after); self.assertIn('park-hyatt-seoul', after)
        later = B.active_closed(_dt.date(2026, 11, 13))
        self.assertNotIn('park-hyatt-seoul', later); self.assertIn('grand-josun-busan', later)
        self.assertNotIn('orakai-cheonggyesan', B.active_closed(_dt.date(2026, 12, 15)))

    def test_closed_on_field_and_guide(self):
        slugs = {v['slug'] for v in self.venues}
        self.assertTrue(set(B.active_closed(self.today)) <= slugs)
        self.assertIn('grand-josun-busan', B.CLOSED_ALL)
        for c in B.active_closed(self.today).values():
            self.assertTrue(c['closed'] and c['url'].startswith('http') and c['source'])
            B.ymd(c['checked'])
            self.assertNotIn('naver', c['url'])
        guide = self.site.pages['/guides/closed-days-korea/']
        table = guide.split('<tbody>', 1)[1].split('</tbody>', 1)[0]
        self.assertIn('Only places we’ve confirmed', guide)
        for v in self.venues:
            page = self.site.pages[f"/finder/{v['slug']}/"]
            self.assertIn('<th>Closed on</th>', page)
            self.assertEqual(f'href="/finder/{v["slug"]}/"' in table, v['slug'] in B.active_closed(self.today), v['slug'])
            if v['slug'] in B.active_closed(self.today):
                self.assertIn(B.dot(B.ymd(B.active_closed(self.today)[v['slug']]['checked'])), page)
            else:
                self.assertIn('Check before you go', page)

    def test_upstream_checklists_show_unconfirmed_conditions(self):
        for v in self.venues:
            values = B.FOREIGNER.get(v['slug'], {})
            keys = B.SAUNA_PKG_KEYS if v['kind_key'] == 'sauna' and 'public_access' in values else B.FCHK_KEYS
            checklist = B.fchk(v, self.today)
            self.assertIn('Foreigner &amp; visitor access checklist', checklist)
            self.assertEqual(checklist.count('<tr>'), len(keys))
            for key, label in keys:
                value = values.get(key, '')
                if not B._is_confirmed(value):
                    self.assertIn(f'<tr><th>{B.E(label)}</th><td class="unk">Not confirmed</td></tr>', checklist)
                elif not B.price_or_schedule(key, value):
                    cell = f'<td>{B.E(value)}{B.fact_provenance(v, value, self.today)}</td>'
                    self.assertIn(f'<tr><th>{B.E(label)}</th>{cell}</tr>', checklist)

    def test_visitor_guide_tables_account_for_each_candidate(self):
        for slug, kind, headers in (
                ('tattoo-friendly-saunas-korea', 'sauna', ('Tattoo rule',)),
                ('hotel-saunas-korea-non-guests', 'sauna', ('Open to visitors?',)),
                ('crossfit-hyrox-drop-in-korea', 'box', ('Drop-in', 'English'))):
            page = self.site.pages[f'/guides/{slug}/']
            tables = re.findall(r'<table class="facts">(.*?)</table>(<p class="muted">.*?</p>)?', page, re.S)
            self.assertEqual(len(tables), len(headers))
            candidates = {f'/finder/{v["slug"]}/' for v in self.venues if v['kind_key'] == kind}
            for (table, note), header in zip(tables, headers):
                with self.subTest(guide=slug, column=header):
                    self.assertIn(f'<th>{header}</th>', table)
                    rows = re.findall(r'<tr>.*?</tr>', table.split('<tbody>', 1)[1], re.S)
                    links = re.findall(r'href="(/finder/[^"]+)"', table)
                    self.assertEqual(len(rows), len(set(links)))
                    self.assertEqual(len(rows), len(links))
                    self.assertLessEqual(set(links), candidates & self.site.pages.keys())
                    for entry in rows:
                        self.assertIn('Checked ', entry)
                        self.assertRegex(entry, r'<a href="https?://[^"]+"[^>]*>Official source · ')
                    pending = len(candidates) - len(rows)
                    if pending:
                        noun = 'venue' if pending == 1 else 'venues'
                        self.assertIn(f'{pending} {noun} pending official confirmation (확인 중)', note)
                        self.assertIn('<a href="/finder/">Finder</a>', note)
                    else:
                        self.assertEqual(note, '')

    def test_cards_use_three_groups_and_details_keep_actions_and_safety(self):
        for v in self.venues:
            card = B.venue_card(v, self.today)
            self.assertEqual(card.count('<dt>'), 0)  # headline-only list cards (PR #62)
            self.assertIn(B.price_short(v), card)
            self.assertNotIn('<table', card)
            self.assertIn(f'href="/finder/{v["slug"]}/"', card)
            self.assertIn('fact-source', card)
            self.assertIn('Checked ', card)
            self.assertIn('card-placeholder' if B.venue_image_path(v) is None else f'/assets/venue-sketch/{v["slug"]}.', card)
            page = self.site.pages[f'/finder/{v["slug"]}/']
            self.assertIn(B.E(B.map_url(v)), page)
            self.assertLess(page.index('detail-actions'), page.index('detail-glance'))
            self.assertLess(page.index('detail-glance'), page.index('<table class="facts">'))
            if v['kind_key'] == 'sauna':
                self.assertIn(B.SAFETY, page)
        v = {**self.venues[0], 'last_verified': self.today - dt.timedelta(days=91)}
        self.assertIn('Outdated', B.v_badges(v, self.today))

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for browser-free interaction checks')
    def test_url_filters_list_sorting_reset_and_empty_results_without_map(self):
        # Build the double from the real generated attributes so JS/data drift fails.
        page = self.site.pages['/finder/']
        elements = Elements(page).elements
        payload = {'script': (ROOT / 'site/static/js/finder.js').read_text(),
                   'elements': elements,
                   'confirmedSlugs': sorted(v['slug'] for v in self.venues if v['price'])}
        run = subprocess.run(['node', '-e', JS_CHECKS], input=json.dumps(payload), text=True, capture_output=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('interaction checks OK', run.stdout)


FINDER_HARNESS = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
class Element {
  constructor(attrs = {}) {
    this.attrs = {...attrs}; this.dataset = {}; this.children = []; this.events = {};
    this.textContent = ''; this.type = attrs.type || ''; this.value = ''; this.checked = false;
    Object.entries(attrs).forEach(([key, val]) => { if (key.startsWith('data-')) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = val; });
  }
  get hidden() { return this.hasAttribute('hidden'); }
  set hidden(value) { if (value) this.attrs.hidden = ''; else delete this.attrs.hidden; }
  hasAttribute(key) { return key in this.attrs; }
  setAttribute(key, val) { this.attrs[key] = val; }
  addEventListener(name, fn) { (this.events[name] ||= []).push(fn); }
  fire(name, props = {}) { (this.events[name] || []).forEach(fn => fn({preventDefault() {}, ...props})); }
  appendChild(node) { this.children = this.children.filter(n => n !== node); this.children.push(node); }
  closest() { return this.group; }
  querySelectorAll() { return this.children; }
  scrollIntoView() { throw new Error('Explorer must never force scrolling'); }
}
function boot(search = '', missing = []) {
  const ids = {}, cards = [], buttons = [], resets = [];
  let form, group;
  input.elements.forEach(([tag, attrs]) => {
    if (missing.includes(attrs.id) || missing.includes(attrs.class)) return;
    const el = new Element(attrs);
    if (attrs.id) ids[attrs.id] = el;
    if ('data-slug' in attrs) cards.push(el);
    if ('data-reset' in attrs) resets.push(el);
    if ('data-seg' in attrs) group = el;
    if (tag === 'button' && 'data-v' in attrs) { el.group = group; buttons.push(el); }
    if (attrs.class === 'filters') form = el;
  });
  if (ids['finder-list']) ids['finder-list'].children = cards.slice();
  const window = new Element();
  const location = {pathname: '/finder/', search, hash: '#main'};
  const history = {replaceState(a, b, url) { const parsed = new URL(url, 'https://example.invalid'); location.search = parsed.search; location.hash = parsed.hash; }};
  const document = {getElementById: id => ids[id] || null,
    querySelector: selector => selector === '.filters' ? form : null,
    querySelectorAll: selector => {
      if (selector === '[data-reset]') return resets;
      if (selector === '[data-seg] button') return buttons;
      const match = selector.match(/data-seg="(city|type)"/);
      if (match) return buttons.filter(b => b.group.dataset.seg === match[1]);
      throw new Error('Unhandled selector: ' + selector);
    }};
  vm.runInNewContext(input.script, {document, window, location, history, URLSearchParams});
  const visible = () => cards.filter(c => !c.hidden);
  const click = (name, value) => buttons.find(b => b.group.dataset.seg === name && b.dataset.v === value).fire('click');
  const change = (key, value) => { const field = ids['f-' + key]; if (field.type === 'checkbox') field.checked = value; else field.value = value; field.fire(key === 'q' ? 'input' : 'change'); };
  const reset = () => resets[0].fire('click');
  return {ids, cards, buttons, visible, click, change, reset, location, window, form};
}
'''

JS_CHECKS = FINDER_HARNESS + r'''
const s = boot();
assert.ok(s.cards.length > 0);
assert.equal(s.visible().length, s.cards.length);
assert.equal(s.ids['finder-list'].hidden, false);
assert.equal(s.ids['f-empty'].hidden, true);
for (const city of ['seoul','busan','jeju','gyeonggi','']) {
  s.click('city', city);
  assert.equal(s.visible().length, s.cards.filter(c => !city || c.dataset.city === city).length);
  s.buttons.filter(b => b.group.dataset.seg === 'city').forEach(b => assert.equal(b.attrs['aria-pressed'], String(b.dataset.v === city)));
}
s.click('type', 'sauna');
assert.equal(s.visible().length, s.cards.filter(c => c.dataset.type === 'sauna').length);
s.change('sunday', 'yes');
assert.equal(s.visible().length, s.cards.filter(c => c.dataset.type === 'sauna' && c.dataset.sunday === 'yes').length);
s.reset();
for (const key of ['cf', 'hyrox', 'price']) {
  s.change(key, true);
  assert.equal(s.visible().length, s.cards.filter(c => c.dataset[key] === '1').length);
  s.reset();
}
s.change('q', '  BUSAN  ');
assert.equal(s.visible().length, s.cards.filter(c => c.dataset.name.includes('busan')).length);
s.change('q', 'no matching venue fixture');
assert.equal(s.visible().length, 0); assert.equal(s.ids['f-empty'].hidden, false);
assert.equal(s.ids['f-count'].textContent, '0 places');
assert.equal(s.ids['finder-list'].hidden, false);
s.reset();
assert.equal(s.visible().length, s.cards.length);
assert.equal(s.location.search, ''); assert.equal(s.location.hash, '#main');
s.change('sub', 'hotel'); s.change('price', true); s.change('sort', 'name');
assert.equal(s.ids['f-more-count'].textContent, '(3 applied)');
assert.equal(s.visible().length, s.cards.filter(c => c.dataset.sub === 'hotel' && c.dataset.price === '1').length);
assert.ok(s.location.search.includes('sub=hotel'));
const titles = s.ids['finder-list'].children.map(c => c.dataset.title);
assert.deepEqual(titles, titles.slice().sort((a,b) => a.localeCompare(b, 'en')));
s.reset();
assert.deepEqual(s.ids['finder-list'].children, s.cards);
const priced = boot('?price=1');
assert.equal(priced.ids['f-price'].checked, true);
assert.deepEqual(priced.visible().map(c => c.dataset.slug).sort(), input.confirmedSlugs);
priced.reset();
assert.deepEqual(priced.visible(), priced.cards);
assert.equal(priced.location.search, '');
const restored = boot('?city=seoul&type=sauna&price=1&sort=date');
assert.equal(restored.visible().length, restored.cards.filter(c => c.dataset.city === 'seoul' && c.dataset.type === 'sauna' && c.dataset.price === '1').length);
assert.equal(restored.ids['f-more-count'].textContent, '(2 applied)');
assert.equal(restored.ids['f-price'].checked, true);
const dates = restored.ids['finder-list'].children.map(c => c.dataset.date);
assert.deepEqual(dates, dates.slice().sort((a,b) => b.localeCompare(a)));
restored.location.search = '?city=jeju'; restored.window.fire('popstate');
assert.equal(restored.visible().length, restored.cards.filter(c => c.dataset.city === 'jeju').length);
assert.equal(restored.ids['f-more-count'].textContent, '');
const invalid = boot('?city=invalid&sort=invalid&price=yes');
assert.equal(invalid.visible().length, invalid.cards.length);
assert.equal(invalid.ids['f-sort'].value, '');
let prevented = false;
s.form.fire('submit', {preventDefault() { prevented = true; }});
assert.equal(prevented, true);
// The script must also tolerate absent containers and optional controls.
boot('', ['finder-list']);
const minimal = boot('?city=busan', ['filters', 'f-count', 'f-empty', 'f-more-count',
  'f-sub', 'f-sort', 'f-q', 'f-sunday', 'f-price', 'f-hyrox', 'f-cf']);
assert.equal(minimal.visible().length, minimal.cards.filter(c => c.dataset.city === 'busan').length);
minimal.reset(); assert.equal(minimal.visible().length, minimal.cards.length);
console.log('interaction checks OK');
'''


SUNDAY_CHECKS = FINDER_HARNESS + r'''
function check(state, status, slugs) {
  assert.equal(state.ids['f-sunday'].value, status);
  assert.deepEqual(state.visible().map(card => card.dataset.slug).sort(), slugs);
  assert.equal(state.ids['f-count'].textContent, slugs.length + (slugs.length === 1 ? ' place' : ' places'));
}
const yes = ['fixture-sunday-open'];
const no = ['fixture-shim', 'fixture-sunday-closed'];
const unknown = ['fixture-branch', 'fixture-compound', 'fixture-conflict', 'fixture-korean',
  'fixture-reply', 'fixture-reply-closure', 'fixture-temporary'];
for (const [status, slugs] of Object.entries({yes, no, unk: unknown})) {
  check(boot('?sunday=' + status), status, slugs);
}
const combined = boot('?type=box&sunday=no');
check(combined, 'no', ['fixture-shim']);
assert.equal(combined.buttons.find(b => b.group.dataset.seg === 'type' && b.dataset.v === 'box').attrs['aria-pressed'], 'true');
combined.location.search = '?sunday=unk'; combined.window.fire('popstate');
check(combined, 'unk', unknown);
combined.reset();
check(combined, '', [...yes, ...no, ...unknown].sort());
assert.equal(combined.location.search, '');
'''


@pytest.mark.skipif(not shutil.which('node'), reason='Node is needed for browser-free interaction checks')
def test_sunday_deep_links_with_dated_evidence(tmp_path, monkeypatch):
    pages = sunday_fixture_pages(tmp_path, monkeypatch, sunday_fixture_cases())
    payload = {'script': (ROOT / 'site/static/js/finder.js').read_text(),
               'elements': Elements(pages['/finder/']).elements}
    run = subprocess.run(['node', '-e', SUNDAY_CHECKS], input=json.dumps(payload), text=True,
                         capture_output=True, timeout=20)
    assert run.returncode == 0, run.stdout + run.stderr


if __name__ == '__main__':
    unittest.main()


class NoMapHelpers(unittest.TestCase):
    def test_schematic_helpers_and_data_are_removed(self):
        for name in ('venue_map', 'map_zones', 'map_lattice', 'map_drawing', 'map_pin',
                     'pin_glyph', 'city_tabs'):
            self.assertFalse(hasattr(B, name), name)
        self.assertFalse(any(name.startswith('MAP_') for name in vars(B)))
