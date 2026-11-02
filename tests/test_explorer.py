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
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'site'))
import build_site as B

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
        cls.venues, events, *_ = B.load(cls.today, None)
        cls.by_slug = {v['slug']: v for v in cls.venues}
        cls.site = B.Site(cls.today, True)
        B.build(cls.site, cls.venues, events, cls.today)

    def test_shared_explorer_dom_order_and_home_illustrations_last(self):
        home, finder = (self.site.pages[p] for p in ('/', '/finder/'))
        def explorer(page):
            return page[page.index('<section class="venue-map"'):page.index('</noscript>')]
        self.assertEqual(explorer(home), explorer(finder))
        for page in (home, finder):
            positions = [page.index(token) for token in (
                '<section class="venue-map"', 'id="map-selection"', '<form class="filters"',
                'id="f-count"', 'id="finder-list"')]
            self.assertEqual(positions, sorted(positions))
            selection = page[page.index('id="map-selection"'):page.index('<template')]
            self.assertIn('Select a pin to preview.', selection)
            self.assertNotIn('selected-venue', selection)
            self.assertIn('/assets/js/finder.js?v=', page)
        self.assertIn(f'Explore {len(self.venues)} boxes &amp; saunas', home)
        self.assertNotIn('<section', home[home.index('See every drawing ›</a></p></section>') + len('See every drawing ›</a></p></section>'):home.index('</main>')])
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

    def test_only_one_initial_canvas_and_complete_regional_views(self):
        markup = B.venue_map(self.venues)
        maps = [ET.fromstring(m) for m in re.findall(r'<svg class="district-map".*?</svg>', markup)]
        self.assertEqual({m.attrib['data-map-city'] for m in maps}, {'all', 'seoul', 'busan', 'jeju', 'gyeonggi'})
        self.assertEqual([m.attrib['data-map-city'] for m in maps if 'hidden' not in m.attrib], ['all'])
        for canvas in maps:
            vb = [int(n) for n in canvas.attrib['viewBox'].split()]
            self.assertEqual(vb[:3], [0, 0, 400])
            height = vb[3]
            city = canvas.attrib['data-map-city']
            pins = [n for n in canvas.iter('g') if 'data-pin' in n.attrib]
            expected = {v['slug'] for v in self.venues if city == 'all' or v['city_key'] == city}
            self.assertEqual({p.attrib['data-pin'] for p in pins}, expected)
            self.assertEqual(len(pins), len(expected))
            points = []
            for pin in pins:
                slug = pin.attrib['data-pin']
                self.assertEqual(pin.attrib['role'], 'button')
                self.assertEqual(pin.attrib['tabindex'], '0')
                self.assertEqual(pin.attrib['aria-controls'], 'map-preview')
                self.assertIn(self.by_slug[slug]['title'], pin.attrib['aria-label'])
                target = pin.find("rect[@class='pin-target']")
                hit = int(target.attrib['width'])
                self.assertEqual(hit, int(target.attrib['height']))
                # 390px phone: map is ~356px wide for a 400-unit viewBox, so hit >= 44px.
                self.assertGreaterEqual(hit * 356 / 400, 44)
                x, y = map(float, re.findall(r'[\d.]+', pin.attrib['transform']))
                self.assertTrue(hit/2 <= x <= 400-hit/2 and hit/2 <= y <= height-hit/2)
                for px, py in points:
                    self.assertGreaterEqual(((x-px)**2 + (y-py)**2)**0.5, 32)
                    self.assertTrue(abs(x-px) >= hit or abs(y-py) >= hit, f'{city}: hit areas overlap')
                points.append((x, y))
            self.assertIn('map-land', ET.tostring(canvas, encoding='unicode'))
        self.assertIn('Han River', markup)
        self.assertNotIn('map-district', markup)
        self.assertIn('District not confirmed · off-map', markup)
        templates = [a['id'] for tag, a in Elements(markup).elements if tag == 'template']
        self.assertEqual(set(templates), {'preview-' + s for s in self.by_slug})

    def test_seoul_districts_sit_on_their_side_of_the_han_river(self):
        north = {'Jongno', 'Jung-gu', 'Yongsan', 'Mapo', 'Seodaemun', 'Eunpyeong', 'Seongdong', 'Gwangjin', 'Dongdaemun',
                 'Jungnang', 'Seongbuk', 'Gangbuk', 'Dobong', 'Nowon'}
        south = {'Gangseo', 'Yangcheon', 'Guro', 'Geumcheon', 'Yeongdeungpo', 'Dongjak', 'Gwanak', 'Seocho', 'Gangnam',
                 'Songpa', 'Gangdong'}
        _, slots, _, _ = B.map_lattice('seoul')
        for key, points in slots.items():
            self.assertIn(key, north | south)
            for _, y in points:
                # pin visual radius 11 must stay clear of the river line
                self.assertTrue(y + 11 < B.MAP_RIVER_Y if key in north else y - 11 > B.MAP_RIVER_Y, key)

    # Approximate district-office longitudes for all 25 Seoul districts.
    SEOUL_LON = {'Gangseo': 126.850, 'Yangcheon': 126.866, 'Guro': 126.888, 'Geumcheon': 126.896, 'Yeongdeungpo': 126.896,
                 'Dongjak': 126.940, 'Gwanak': 126.952, 'Seocho': 127.033, 'Gangnam': 127.047, 'Songpa': 127.106,
                 'Gangdong': 127.124, 'Eunpyeong': 126.929, 'Mapo': 126.902, 'Seodaemun': 126.937, 'Jongno': 126.979,
                 'Jung-gu': 126.998, 'Yongsan': 126.990, 'Seongbuk': 127.017, 'Gangbuk': 127.026, 'Seongdong': 127.037,
                 'Dongdaemun': 127.040, 'Dobong': 127.047, 'Nowon': 127.057, 'Jungnang': 127.093, 'Gwangjin': 127.082}

    def test_seoul_pins_follow_real_west_to_east_order(self):
        self.assertEqual(len(self.SEOUL_LON), 25)
        _, slots, _, _ = B.map_lattice('seoul')
        self.assertLessEqual(set(slots), set(self.SEOUL_LON))
        canvas = re.search(r'<svg class="district-map" data-map-city="seoul".*?</svg>', B.venue_map(self.venues))[0]
        xs = {}
        for slug, tx in re.findall(r'data-pin="([^"]+)" transform="translate\(([\d.]+) ', canvas):
            v = self.by_slug[slug]
            loc = (v['location'] + ' ' + v['area']).lower()
            key = next(k for k in slots if k.lower() in loc)
            xs.setdefault(key, []).append(float(tx))
        mean = {k: sum(v) / len(v) for k, v in xs.items()}
        for a in mean:
            for b in mean:
                if self.SEOUL_LON[b] - self.SEOUL_LON[a] > 0.03:
                    self.assertLess(mean[a], mean[b], f'{a} should be west of {b}')

    def test_css_bounds_canvas_and_compacts_mobile_hero(self):
        css = (ROOT / 'site/static/css/site.css').read_text()
        canvas = re.search(r'\.district-map\{([^}]+)', css)[1]
        self.assertIn('height:auto', canvas)
        self.assertIn('max-width:460px', canvas)
        self.assertIn('[hidden]{display:none!important}', css)
        mobile = css[css.rindex('@media(max-width:719px)'):]
        self.assertIn('.home-hero{padding:14px 16px;margin:10px 0 8px}', mobile)
        self.assertNotIn('.map-regions{grid-template-columns:', css)

    def test_default_order_prioritizes_price_then_verification(self):
        elements = Elements(self.site.pages['/finder/']).elements
        actual = [a['data-slug'] for tag, a in elements if tag == 'article' and 'data-slug' in a]
        expected = sorted(self.venues, key=lambda v: (not v['ad'], not bool(v['price']), v['status'] != 'verified', -len(v['facts']), v['order']))
        self.assertEqual(actual, [v['slug'] for v in expected])

    def test_prices_preserve_all_original_terms_even_with_numeric_price(self):
        for v in self.venues:
            for key, value in v['facts']:
                if key.startswith(('Price', 'Drop-in')) and value.strip():
                    self.assertIn(B.E(value), B.price_html(v), v['slug'])
            if v['price']:
                self.assertIn(B.E(v['price']), B.price_html(v))
                if v['price_caveat']:
                    self.assertIn(B.E(v['price_caveat']), B.price_html(v))
        repo = self.by_slug['solo-sauna-repo-noryangjin']
        terms = next(value for key, value in repo['facts'] if key == 'Price')
        page = self.site.pages['/finder/solo-sauna-repo-noryangjin/']
        self.assertIn(B.E(terms), page)
        self.assertIn('60 min from ₩45,000', page)
        self.assertIn('Family room, 2 people', page)
        # List cards and map previews are headline-only (PR #62): full terms live on the venue page.
        for output in (B.venue_card(repo), B.venue_contents(repo, preview=True)):
            self.assertIn(B.price_short(repo), output)
            self.assertNotIn(B.E(terms), output)

    def test_access_age_time_tattoo_and_provenance_remain_verbatim(self):
        mandatory = {'Closed', 'Booking', 'Who can use', 'Conditions', 'Age', 'Capacity', 'Session length',
                     'Time allowed', 'Entry', 'Non-guests', 'Open-air bath rules', 'Types · length'}
        for v in self.venues:
            rows = [row for _, values in B.fact_groups(v) for row in values]
            for key, value in v['facts']:
                if value.strip() and (key.startswith('Hours') or key in mandatory):
                    self.assertIn((key, value), rows, v['slug'])
            self.assertTrue(any(key == 'Tattoos' for key, _ in rows))
            if not B.FOREIGNER.get(v['slug'], {}).get('tattoo') and not any('tattoo' in val.lower() for _, val in v['facts'] + [('Tip', tip) for tip in v['tips']]):
                self.assertIn(('Tattoos', B.UNKNOWN), rows)
        grand = self.by_slug['grand-josun-busan']
        self.assertIn(B.E("Children 12 and under can't enter, for safety."), self.site.pages['/finder/grand-josun-busan/'])
        sisu = self.by_slug['sisu-house']
        for key, value in sisu['facts']:
            if 'reply' in value:
                self.assertIn(B.E(value), B.price_html(sisu) + B.venue_facts(sisu))
        note = B.fact_provenance(sisu, B.FOREIGNER[sisu['slug']]['full_price'])
        self.assertIn('Venue reply', note)
        self.assertIn('2026-10-18', note)
        self.assertNotIn('href=', note)

    def test_pending_reply_and_booking_procedure_use_official_provenance(self):
        grand = self.by_slug['grand-hyatt-seoul']
        zest = self.by_slug['crossfit-zest']
        cases = (
            (grand, B.FOREIGNER[grand['slug']]['restriction'], 'awaiting official hotel reply'),
            (zest, next(value for _, value in zest['facts'] if 'after an email reply' in value),
             'after an email reply'),
        )
        for v, value, phrase in cases:
            with self.subTest(venue=v['slug']):
                self.assertIn(phrase, value)
                note = B.fact_provenance(v, value)
                self.assertNotIn('Venue reply', note)
                self.assertIn(f'Checked {B.dot(v["last_verified"])}', note)
                self.assertIn(f'href="{B.E(v["source"])}"', note)
                self.assertIn(B.E(value) + note, self.site.pages[f'/finder/{v["slug"]}/'])

    def test_shared_official_and_reply_citation_does_not_use_closure_date(self):
        v = self.by_slug['paradise-hotel-busan']
        value = next(value for key, value in v['facts'] if key == 'Closed')
        self.assertIn('October: 10/28 (official page and hotel reply)', value)
        note = B.fact_provenance(v, value)
        self.assertNotIn('Venue reply', note)
        self.assertNotIn('10/28', note)
        self.assertIn(f'Checked {B.dot(v["last_verified"])}', note)
        self.assertIn(f'href="{B.E(v["source"])}"', note)
        self.assertIn(B.E(value) + note, self.site.pages[f'/finder/{v["slug"]}/'])

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
                    note = B.fact_provenance(v, value)
                    self.assertIn(f'Venue reply · {checked} ·', note)
                    self.assertNotIn('Official source', note)
                    self.assertNotIn('href=', note)
                    for output in outputs:
                        self.assertIn(B.E(value) + note, output)

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
                note = B.fact_provenance(v, value)
                self.assertIn(f'Venue reply · {checked}', note)
                self.assertNotIn('10/28', note)
                self.assertNotIn('10/20', note)
                self.assertNotIn('Official source', note)
                self.assertNotIn('href=', note)
        for value in ('Closed 10/28 (awaiting official hotel reply)',
                      'Drop-in (after an email reply from staff)'):
            with self.subTest(value=value):
                note = B.fact_provenance(v, value)
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
                self.assertIn(B.E(value), B.venue_facts(v))
        self.assertNotIn('No. Hotel guests only.', B.venue_contents(v, preview=True))

    def test_sauna_package_fields_reach_cards_previews_and_glance(self):
        packages = [v for v in self.venues if v['kind_key'] == 'sauna' and 'public_access' in B.FOREIGNER.get(v['slug'], {})]
        self.assertEqual(len(packages), 10)
        self.assertEqual(len(B.SAUNA_PKG_KEYS), 11)
        for v in packages:
            groups = dict(B.fact_groups(v))
            glance = self.site.pages[f'/finder/{v["slug"]}/'].split('id="glance-heading"', 1)[1].split('</section>', 1)[0]
            outputs = (glance,)  # cards/previews are headline-only since PR #62
            for key, label in B.SAUNA_PKG_KEYS:
                with self.subTest(venue=v['slug'], key=key):
                    value = B.FOREIGNER[v['slug']][key]
                    value = value if B._is_confirmed(value) else B.UNKNOWN
                    group = 'Hours & Closed' if key in ('last_entry', 'closures') else 'Visitor / Tattoo rules' if key in ('scrub_fee', 'age_tattoo_attire') else 'Access / Booking'
                    self.assertIn((label, value), groups[group])
                    for output in outputs:
                        self.assertIn(f'<strong>{B.E(label)}:</strong> {B.E(value)}', output)
                        if B._is_confirmed(B.FOREIGNER[v['slug']][key]):
                            self.assertIn(B.fact_provenance(v, value), output)

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
        self.assertTrue(set(B.CLOSED) <= slugs)
        self.assertIn('grand-josun-busan', B.CLOSED_ALL)
        for c in B.CLOSED.values():
            self.assertTrue(c['closed'] and c['url'].startswith('http') and c['source'])
            B.ymd(c['checked'])
            self.assertNotIn('naver', c['url'])
        guide = self.site.pages['/guides/closed-days-korea/']
        table = guide.split('<tbody>', 1)[1].split('</tbody>', 1)[0]
        self.assertIn('Only places we’ve confirmed', guide)
        for v in self.venues:
            page = self.site.pages[f"/finder/{v['slug']}/"]
            self.assertIn('<th>Closed on</th>', page)
            self.assertEqual(f'href="/finder/{v["slug"]}/"' in table, v['slug'] in B.CLOSED, v['slug'])
            if v['slug'] in B.CLOSED:
                self.assertIn(B.dot(B.ymd(B.CLOSED[v['slug']]['checked'])), page)
            else:
                self.assertIn('Check before you go', page)

    def test_upstream_checklists_and_guides_exclude_unconfirmed_conditions(self):
        for v in self.venues:
            values = B.FOREIGNER.get(v['slug'], {})
            keys = B.SAUNA_PKG_KEYS if v['kind_key'] == 'sauna' and 'public_access' in values else B.FCHK_KEYS
            checklist = B.fchk(v)
            self.assertIn('Foreigner &amp; visitor access checklist', checklist)
            self.assertEqual(checklist.count('<tr>'), len(keys))
            for key, label in keys:
                cell = f'<td>{B.E(values[key])}</td>' if B._is_confirmed(values.get(key, '')) else '<td class="unk">Not confirmed</td>'
                self.assertIn(f'<tr><th>{label}</th>{cell}</tr>', checklist)
        for slug, kind, key in (
                ('tattoo-friendly-saunas-korea', 'sauna', 'tattoo'),
                ('hotel-saunas-korea-non-guests', 'sauna', 'dropin'),
                ('crossfit-hyrox-drop-in-korea', 'box', 'dropin')):
            page = self.site.pages[f'/guides/{slug}/']
            table = page.split('<tbody>', 1)[1].split('</tbody>', 1)[0]
            for v in self.venues:
                if v['kind_key'] != kind:
                    continue
                confirmed = B._is_confirmed(B.FOREIGNER.get(v['slug'], {}).get(key, ''))
                self.assertEqual(f'href="/finder/{v["slug"]}/"' in table, confirmed, v['slug'])
            self.assertIn('excluded from this table', page)

    def test_cards_use_three_groups_and_details_keep_actions_and_safety(self):
        for v in self.venues:
            card = B.venue_card(v)
            self.assertEqual(card.count('<dt>'), 0)  # headline-only list cards (PR #62)
            self.assertIn(B.price_short(v), card)
            self.assertNotIn('<table', card)
            preview = B.venue_contents(v, preview=True)
            self.assertIn(f'href="/finder/{v["slug"]}/"', preview)
            self.assertIn(B.E(B.map_url(v)), preview)
            self.assertIn(B.E(v['source']), preview)
            self.assertIn(B.dot(v['last_verified']), preview)
            page = self.site.pages[f'/finder/{v["slug"]}/']
            self.assertLess(page.index('detail-actions'), page.index('detail-glance'))
            self.assertLess(page.index('detail-glance'), page.index('<table class="facts">'))
            if v['kind_key'] == 'sauna':
                self.assertIn(B.SAFETY, page)
        v = {**self.venues[0], 'stale': True}
        self.assertIn('오래됨', B.v_badges(v))

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for browser-free interaction checks')
    def test_url_filters_canvas_sync_keyboard_selection_and_empty_results(self):
        # Build the double from the real generated attributes so JS/data drift fails.
        page = self.site.pages['/finder/']
        elements = Elements(page).elements
        payload = {'script': (ROOT / 'site/static/js/finder.js').read_text(),
                   'cards': [a for tag, a in elements if 'data-slug' in a],
                   'maps': []}
        for markup in re.findall(r'<svg class="district-map".*?</svg>', page):
            attrs = Elements(markup).elements
            payload['maps'].append({'attrs': attrs[0][1], 'pins': [a for _, a in attrs if 'data-pin' in a]})
        run = subprocess.run(['node', '-e', JS_CHECKS], input=json.dumps(payload), text=True, capture_output=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('interaction checks OK', run.stdout)


JS_CHECKS = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
class Element {
  constructor(attrs = {}, svg = false) {
    this.attrs = {...attrs}; this.dataset = {}; this.children = []; this.events = {}; this.svg = svg;
    this.textContent = ''; this.type = ''; this.value = ''; this.checked = false;
    Object.entries(attrs).forEach(([key, val]) => { if (key.startsWith('data-')) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = val; });
    this.classList = {toggle: (name, on) => { this[name] = on; }};
  }
  get hidden() { return this.svg ? undefined : this.hasAttribute('hidden'); }
  set hidden(value) { if (!this.svg) this.toggleAttribute('hidden', value); }
  hasAttribute(key) { return key in this.attrs; }
  setAttribute(key, val) { this.attrs[key] = val; }
  toggleAttribute(key, on) { if (on) this.attrs[key] = ''; else delete this.attrs[key]; }
  addEventListener(name, fn) { (this.events[name] ||= []).push(fn); }
  fire(name, props = {}) { (this.events[name] || []).forEach(fn => fn({preventDefault() {}, ...props})); }
  replaceChildren() { this.children = []; }
  appendChild(node) { this.children = this.children.filter(n => n !== node); this.children.push(node); }
  closest(selector) { return selector === '[data-map-city]' ? this.region : this.group; }
  querySelectorAll(selector) { return this.children; }
  focus() { focused = this; }
  scrollIntoView() { throw new Error('Explorer must never force scrolling'); }
}
let focused;
function boot(search = '') {
  const ids = {};
  ['finder-list','map-preview','map-selection','map-view','f-count','f-empty','f-more-count',
   'f-sub','f-sort','f-q','f-sunday','f-price','f-hyrox','f-cf'].forEach(id => ids[id] = new Element());
  ['price','hyrox','cf'].forEach(key => ids['f-' + key].type = 'checkbox');
  const cards = input.cards.map(attrs => new Element(attrs));
  cards.forEach(c => { ids['preview-' + c.dataset.slug] = {content: {cloneNode: () => ({slug: c.dataset.slug})}}; });
  ids['finder-list'].children = cards.slice();
  const regions = input.maps.map(m => { const region = new Element(m.attrs, true); region.children = m.pins.map(attrs => { const pin = new Element(attrs, true); pin.region = region; return pin; }); return region; });
  const pins = regions.flatMap(r => r.children);
  const buttons = [];
  const addButtons = (groupName, values) => {
    const group = new Element({'data-seg': groupName});
    values.forEach(value => { const button = new Element({'data-v': value}); button.group = group; buttons.push(button); });
  };
  addButtons('city', ['', 'seoul', 'busan', 'jeju', 'gyeonggi']);
  addButtons('city', ['', 'seoul', 'busan', 'jeju', 'gyeonggi']);
  addButtons('type', ['', 'box', 'sauna']);
  const form = new Element(), reset = new Element(); const window = new Element();
  const location = {pathname: '/finder/', search, hash: '#main'};
  const history = {replaceState(a, b, url) { const parsed = new URL(url, 'https://example.invalid'); location.search = parsed.search; }};
  const document = {getElementById: id => ids[id], createElement: () => new Element(),
    querySelector: selector => selector === '.filters' ? form : null,
    querySelectorAll: selector => {
      if (selector === '[data-pin]') return pins;
      if (selector === '[data-map-city]') return regions;
      if (selector === '[data-reset]') return [reset];
      if (selector === '[data-seg] button') return buttons;
      const group = selector.match(/data-seg="(city|type)"/);
      if (group) return buttons.filter(b => b.group.dataset.seg === group[1]);
      throw new Error('Unhandled selector: ' + selector);
    }};
  vm.runInNewContext(input.script, {document, window, location, history, URLSearchParams});
  const active = () => regions.filter(r => !r.hasAttribute('hidden'));
  const visible = () => cards.filter(c => !c.hidden);
  const clickCity = (city, index = 0) => buttons.filter(b => b.group.dataset.seg === 'city' && b.dataset.v === city)[index].fire('click');
  const change = (key, value) => { const field = ids['f-' + key]; if (field.type === 'checkbox') field.checked = value; else field.value = value; field.fire(key === 'q' ? 'input' : 'change'); };
  return {ids, cards, pins, buttons, regions, active, visible, clickCity, change, reset, location, window};
}
const s = boot();
assert.equal(s.active().length, 1); assert.equal(s.active()[0].dataset.mapCity, 'all');
assert.equal(s.ids['map-selection']['has-selection'], false);
assert.equal(s.ids['map-preview'].children.length, 0);
assert.equal(s.visible().length, input.cards.length);
for (const city of ['seoul','busan','jeju','gyeonggi','']) {
  s.clickCity(city, 1); // basic filter -> map
  assert.equal(s.active().length, 1); assert.equal(s.active()[0].dataset.mapCity, city || 'all');
  assert.equal(s.visible().length, input.cards.filter(c => !city || c['data-city'] === city).length);
  s.buttons.filter(b => b.group.dataset.seg === 'city').forEach(b => assert.equal(b.attrs['aria-pressed'], String(b.dataset.v === city)));
  const livePins = s.pins.filter(p => p.attrs.tabindex === '0');
  assert.equal(livePins.length, s.visible().length);
  assert.ok(livePins.every(p => !p.region.hasAttribute('hidden')));
}
s.clickCity('busan'); // map -> basic filter
let pin = s.active()[0].children[0]; pin.fire('keydown', {key: 'Enter'});
assert.equal(s.ids['map-selection']['has-selection'], true);
assert.equal(s.ids['map-preview'].children[0].slug, pin.dataset.pin);
pin.fire('keydown', {key: 'ArrowRight'}); assert.equal(focused, s.active()[0].children[1]);
pin.fire('keydown', {key: 'End'}); assert.equal(focused, s.active()[0].children.at(-1));
pin.fire('keydown', {key: 'Home'}); assert.equal(focused, pin);
s.change('q', 'no matching venue fixture');
assert.equal(s.visible().length, 0); assert.equal(s.ids['f-empty'].hidden, false);
assert.equal(s.ids['map-selection']['has-selection'], false);
assert.equal(s.ids['f-count'].textContent, '0 places');
s.reset.fire('click');
assert.equal(s.visible().length, input.cards.length); assert.equal(s.active()[0].dataset.mapCity, 'all');
assert.equal(s.ids['map-selection']['has-selection'], false);
s.change('sub', 'hotel'); s.change('price', true); s.change('sort', 'name');
assert.equal(s.ids['f-more-count'].textContent, '(3 applied)');
assert.ok(s.visible().every(c => c.dataset.sub === 'hotel' && c.dataset.price === '1'));
assert.ok(s.location.search.includes('sub=hotel'));
const titles = s.ids['finder-list'].children.map(c => c.dataset.title);
assert.deepEqual(titles, titles.slice().sort((a,b) => a.localeCompare(b, 'en')));
const restored = boot('?city=seoul&type=sauna&price=1&sort=date');
assert.equal(restored.active()[0].dataset.mapCity, 'seoul');
assert.ok(restored.visible().every(c => c.dataset.city === 'seoul' && c.dataset.type === 'sauna' && c.dataset.price === '1'));
assert.equal(restored.ids['f-more-count'].textContent, '(2 applied)');
assert.equal(restored.ids['map-selection']['has-selection'], false);
restored.location.search = '?city=jeju'; restored.window.fire('popstate');
assert.equal(restored.active()[0].dataset.mapCity, 'jeju');
assert.equal(restored.ids['f-more-count'].textContent, '');
const invalid = boot('?city=invalid&sort=invalid&price=yes');
assert.equal(invalid.active()[0].dataset.mapCity, 'all'); assert.equal(invalid.visible().length, input.cards.length);
console.log('interaction checks OK');
'''

if __name__ == '__main__':
    unittest.main()


class AllKoreaZones(unittest.TestCase):
    def test_all_korea_view_outlines_each_region_without_overlap(self):
        zones = re.findall(r'<rect class="map-zone map-zone-(\w+)" x="(-?\d+)" y="(-?\d+)" width="(\d+)" height="(\d+)"', B.map_zones('all'))
        self.assertEqual({z[0] for z in zones}, {'seoul', 'busan', 'jeju', 'gyeonggi'})
        self.assertEqual(B.map_zones('seoul'), '')
        boxes = [(k, int(x), int(y), int(x) + int(w), int(y) + int(h)) for k, x, y, w, h in zones]
        for i, a in enumerate(boxes):
            self.assertTrue(8 <= a[1] and a[3] <= 392, a)
            for b in boxes[i + 1:]:
                if a[0] != b[0]:
                    self.assertFalse(a[1] < b[3] and b[1] < a[3] and a[2] < b[4] and b[2] < a[4], (a, b))
        _, slots, _, _ = B.map_lattice('all')
        for k, pts in slots.items():  # every pin sits inside one of its region's zones
            for x, y in pts:
                self.assertTrue(any(z[0] == k and z[1] < x < z[3] and z[2] < y < z[4] for z in boxes), (k, x, y))
