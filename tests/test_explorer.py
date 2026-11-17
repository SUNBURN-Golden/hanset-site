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

    def test_default_order_prioritizes_price_then_verification(self):
        elements = Elements(self.site.pages['/finder/']).elements
        actual = [a['data-slug'] for tag, a in elements if tag == 'article' and 'data-slug' in a]
        expected = sorted(self.venues, key=lambda v: (not v['ad'], not bool(v['price']), v['status'] != 'verified', -len(v['facts']), v['order']))
        self.assertEqual(actual, [v['slug'] for v in expected])

    def test_prices_preserve_all_original_terms_even_with_numeric_price(self):
        for v in self.venues:
            for key, value in v['facts']:
                if key.startswith(('Price', 'Drop-in')) and value.strip():
                    self.assertIn(B.E(B.display_value(v, key, value)), B.price_html(v, self.today), v['slug'])
            if v['price']:
                self.assertIn(B.E(v['price']), B.price_html(v, self.today))
                if v['price_caveat']:
                    self.assertIn(B.E(v['price_caveat']), B.price_html(v, self.today))
        repo = self.by_slug['solo-sauna-repo-noryangjin']
        terms = next(value for key, value in repo['facts'] if key == 'Price')
        page = self.site.pages['/finder/solo-sauna-repo-noryangjin/']
        self.assertIn(B.E(terms), page)
        self.assertIn('60 min from ₩45,000', page)
        self.assertIn('Family room, 2 people', page)
        # List cards are headline-only (PR #62): full terms live on the venue page.
        for output in (B.venue_card(repo, self.today), B.venue_contents(repo, self.today)):
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
                self.assertIn(B.E(value), B.price_html(sisu, self.today) + B.venue_facts(sisu, self.today))
        note = B.fact_provenance(sisu, B.FOREIGNER[sisu['slug']]['full_price'], self.today)
        self.assertIn('Venue reply', note)
        self.assertIn('2026-10-18', note)
        self.assertNotIn('href=', note)

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
        note = B.fact_provenance(v, value, self.today)
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
                    note = B.fact_provenance(v, value, self.today)
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
                self.assertIn(B.E(value), B.venue_facts(v, self.today))
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
                    for output in outputs:
                        self.assertIn(f'<strong>{B.E(label)}:</strong> {B.E(value)}', output)
                        if B._is_confirmed(B.FOREIGNER[v['slug']][key]):
                            self.assertIn(B.fact_provenance(v, value, self.today), output)

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

    def test_upstream_checklists_and_guides_exclude_unconfirmed_conditions(self):
        for v in self.venues:
            values = B.FOREIGNER.get(v['slug'], {})
            keys = B.SAUNA_PKG_KEYS if v['kind_key'] == 'sauna' and 'public_access' in values else B.FCHK_KEYS
            checklist = B.fchk(v, self.today)
            self.assertIn('Foreigner &amp; visitor access checklist', checklist)
            self.assertEqual(checklist.count('<tr>'), len(keys))
            for key, label in keys:
                cell = f'<td>{B.E(values[key])}{B.fact_provenance(v, values[key], self.today)}</td>' if B._is_confirmed(values.get(key, '')) else '<td class="unk">Not confirmed</td>'
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
                   'elements': elements}
        run = subprocess.run(['node', '-e', JS_CHECKS], input=json.dumps(payload), text=True, capture_output=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('interaction checks OK', run.stdout)


JS_CHECKS = r'''
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

if __name__ == '__main__':
    unittest.main()


class NoMapHelpers(unittest.TestCase):
    def test_schematic_helpers_and_data_are_removed(self):
        for name in ('venue_map', 'map_zones', 'map_lattice', 'map_drawing', 'map_pin',
                     'pin_glyph', 'city_tabs'):
            self.assertFalse(hasattr(B, name), name)
        self.assertFalse(any(name.startswith('MAP_') for name in vars(B)))
