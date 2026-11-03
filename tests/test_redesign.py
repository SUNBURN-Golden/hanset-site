"""Discovery contracts for the final redesign (TASK changes 1–8)."""
from copy import deepcopy
import datetime as dt
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'site'))
import build_site as B

ROOT = Path(__file__).resolve().parents[1]


class Tags(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.tags = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


@pytest.fixture(scope='module')
def inventory():
    today = dt.date(2026, 11, 3)
    venues, events, *_ = B.load(today, None)
    site = B.Site(today, True)
    B.build(site, venues, events, today)
    return venues, events, site


def cards(page):
    return [attrs for tag, attrs in Tags(page).tags if tag == 'article' and 'data-slug' in attrs]


def test_home_has_only_brand_search_switch_and_contact_at_rest(inventory):
    _, _, site = inventory
    page = site.pages['/']
    tags = Tags(page).tags
    assert page.count('<h1>HANSET, Sweat on Korea</h1>') == 1
    inputs = [attrs for tag, attrs in tags if tag == 'input']
    assert len(inputs) == 1
    assert inputs[0]['placeholder'] == 'Where do you want to sweat in Korea?'
    assert inputs[0]['name'] == 'q'
    assert 'action="/finder/"' in page
    assert '<header' not in page and '<nav' not in page
    assert '<article' not in page and '<img' not in page and '<svg' not in page
    assert 'category-grid' not in page and 'filters' not in page
    assert 'id="search-results" hidden' in page
    assert all('hidden' in attrs for tag, attrs in tags if tag == 'li')
    footer = page.split('<footer', 1)[1].split('</footer>', 1)[0]
    assert footer.count('<a ') == 1
    assert 'mailto:hanset@soulbound.co.kr' in footer
    assert 'href="/sauna/">Sweat' in page
    assert 'href="/magazine/">Magazine' in page
    switch = next(attrs for tag, attrs in tags if attrs.get('class') == 'home-switch')
    assert switch['role'] == 'group' and switch['aria-label'] == 'Browse'


def test_memberships_counts_and_all_existing_details(inventory):
    venues, _, site = inventory
    expected = {'sauna': 39, 'crossfit': 9, 'hyrox': 3, 'gym': 1}
    memberships = {}
    for key, count in expected.items():
        page = site.pages[f'/{key}/']
        actual = {card['data-slug'] for card in cards(page)}
        memberships[key] = actual
        assert len(actual) == count
        assert actual == {v['slug'] for v in venues if key in B.venue_categories(v)}
        assert f'{count} {"place" if count == 1 else "places"}' in page
        assert f'<link rel="canonical" href="{B.SITE_URL}/{key}/">' in page
        assert 'noindex' not in page
    assert memberships['hyrox'] == {'crossfit-hannam', 'heyday-cultures-crossfit', 'holis-training-club'}
    assert memberships['gym'] == {'holis-training-club'}
    assert len(memberships['hyrox'] & memberships['crossfit']) == 2
    assert len(cards(site.pages['/finder/'])) == len(venues) == 49
    assert all(f'/finder/{v["slug"]}/' in site.pages for v in venues)
    assert 'basalt-crossfit' not in memberships['hyrox']
    assert 'holis-training-club' in memberships['hyrox']  # class evidence, not directory affiliation
    assert 'HYROX official gym list verified' not in site.pages['/finder/holis-training-club/']


def test_hyrox_requires_literal_published_flag_and_gym_requires_explicit_kind(inventory):
    venues, _, _ = inventory
    v = deepcopy(next(v for v in venues if v['slug'] == 'crossfit-hannam'))
    for flag in (False, None, 'true', 'yes', 1):
        v['c']['hyrox'] = flag
        assert 'hyrox' not in B.venue_categories(v)
    v['c']['hyrox'] = True
    assert 'hyrox' in B.venue_categories(v)
    v['title'] = 'An invented Gym name'
    assert 'gym' not in B.venue_categories(v)
    holis = deepcopy(next(v for v in venues if v['slug'] == 'holis-training-club'))
    holis['facts'] = [('Open gym', 'Unconfirmed')]
    assert 'gym' not in B.venue_categories(holis)


def test_empty_gym_and_magazine_remain_reachable(inventory):
    venues, events, _ = inventory
    body = B.category_body('gym', 'Gym', [])
    assert 'Coming soon' in body and '0 places' in body
    assert not cards(body) and 'No matching places' not in body
    site = B.Site(dt.date(2026, 11, 3), True)
    with patch.object(B, 'ARTICLES', []):
        B.build(site, venues, events, site.today)
    assert '/magazine/' in site.pages
    assert 'Coming soon' in site.pages['/magazine/']
    assert 'Articles are currently in Korean' not in site.pages['/magazine/']


def test_five_navigation_links_and_single_active_link(inventory):
    venues, _, site = inventory
    for path, page in site.pages.items():
        if path == '/':
            continue
        nav = re.search(r'<nav class="section-tabs".*?</nav>', page)[0]
        links = [attrs for tag, attrs in Tags(nav).tags if tag == 'a']
        assert [attrs['href'] for attrs in links] == [f'/{key}/' for key, _ in B.TABS]
        current = [attrs['href'] for attrs in links if attrs.get('aria-current') == 'page']
        expected = []
        if path.startswith('/magazine/'):
            expected = ['/magazine/']
        elif path in [f'/{key}/' for key, _ in B.TABS]:
            expected = [path]
        elif path.startswith('/finder/') and path != '/finder/':
            venue = next(v for v in venues if path == f'/finder/{v["slug"]}/')
            expected = [f'/{next(k for k in ("sauna", "crossfit", "gym", "hyrox") if k in B.venue_categories(venue))}/']
        assert current == expected, path
        assert nav.count('<svg ') == 5
        assert nav.count('aria-hidden="true"') == 5


def test_search_index_is_deduplicated_multilingual_and_escaped(inventory):
    venues, events, site = inventory
    tags = Tags(site.pages['/']).tags
    records = [attrs for tag, attrs in tags if 'data-search' in attrs]
    assert len(records) == 49
    urls = [attrs['href'] for tag, attrs in tags if tag == 'a' and attrs.get('href', '').startswith('/finder/')]
    assert len(urls) == len(set(urls)) == 49
    assert set(urls) == {f'/finder/{v["slug"]}/' for v in venues}
    for v in venues:
        text = B.search_text(v)
        assert v['title'].lower() in text and v['title_ko'].lower() in text
        assert v['city'].lower() in text and v['kind'].lower() in text
    malicious = deepcopy(venues)
    malicious[0]['title'] = '<img src=x onerror="attack()"> & Test'
    safe = B.Site(site.today, True)
    B.build(safe, malicious, events, site.today)
    parsed = Tags(safe.pages['/']).tags
    assert not any(tag == 'img' for tag, _ in parsed)
    assert any('<img src=x' in attrs.get('data-search', '') for _, attrs in parsed)
    assert 'onerror="attack()"' not in safe.pages['/']


def test_venue_name_is_loaded_and_title_is_a_safe_fallback(inventory):
    venues, _, _ = inventory
    assert all(v['name'] for v in venues)
    v = deepcopy(venues[0])
    del v['name']
    assert v['title'].lower() in B.search_text(v)
    assert B.E(v['title']) in B.venue_card(v)


def test_missing_images_are_plain_placeholders_and_cards_stay_minimal(inventory, tmp_path):
    venues, _, _ = inventory
    with patch.object(B, 'VENUE_SKETCH', tmp_path):
        markup_by_slug = {v['slug']: B.venue_card(v) for v in venues}
    for v in venues:
        markup = markup_by_slug[v['slug']]
        assert markup.count('card-placeholder') == 1
        assert '<img' not in markup and '<svg' not in markup and '<picture' not in markup
        assert B.E(v['title']) in markup and B.E(v['kind']) in markup
        assert 'card-facts' not in markup and '<table' not in markup
        assert 'fact-source' not in markup and 'Official reference' not in markup
        assert markup.count('class="badge ') <= 3
        if v['price']:
            assert B.E(v['price_label']) in markup and B.E(v['price']) in markup
        else:
            assert 'Price: Not confirmed' not in markup
    assert not (ROOT / 'site/static/img/cards').exists()


def test_public_routes_assets_and_local_only_requests(inventory):
    _, _, site = inventory
    assert '/ads/' not in site.pages
    assert '/404.html' in site.pages
    assert (ROOT / 'site/static/root/google1cc2d472584fa095.html').is_file()
    for path, page in site.pages.items():
        assert re.search(r'/assets/css/site.css\?v=[a-f0-9]+', page)
        for tag, attrs in Tags(page).tags:
            if tag in ('script', 'img', 'source', 'iframe'):
                assert not attrs.get('src', '').startswith(('http:', 'https:', '//')), path
            if tag == 'link' and attrs.get('rel') in ('stylesheet', 'preload'):
                assert attrs['href'].startswith('/'), path
    for script in ('search.js', 'finder.js'):
        source = (ROOT / 'site/static/js' / script).read_text()
        assert 'fetch(' not in source and 'innerHTML' not in source
        assert 'localStorage' not in source and 'XMLHttpRequest' not in source


def test_home_search_interactions(inventory):
    _, _, site = inventory
    data = [attrs['data-search'] for _, attrs in Tags(site.pages['/']).tags if 'data-search' in attrs]
    payload = {'script': (ROOT / 'site/static/js/search.js').read_text(), 'records': data}
    result = subprocess.run(['node', '-e', SEARCH_CHECK], input=json.dumps(payload), text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


SEARCH_CHECK = r'''
const assert = require('node:assert/strict'), vm = require('node:vm');
const data = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
class Element {
  constructor() { this.events = {}; this.hidden = false; this.value = ''; this.textContent = ''; }
  addEventListener(key, fn) { this.events[key] = fn; }
  fire(key, event = {}) { this.events[key](event); }
}
const input = new Element(), results = new Element(), count = new Element(), form = new Element();
input.form = form;
const records = data.records.map(text => Object.assign(new Element(), {dataset: {search: text}}));
results.querySelectorAll = () => records;
const document = {getElementById: id => ({'home-q': input, 'search-results': results, 'search-count': count}[id])};
const location = {pathname: '/', search: '', hash: ''}, window = new Element();
const history = {replaceState(_a, _b, url) { location.search = new URL(url, 'https://local.test').search; }};
vm.runInNewContext(data.script, {document, location, history, window, URLSearchParams});
const visible = () => records.filter(record => !record.hidden);
assert.equal(results.hidden, true); assert.equal(visible().length, 0);
for (const query of ['HANNAM', '부산', 'Hotel Sauna', ' HYROX   Seoul ', '크로스핏']) {
  input.value = query; input.fire('input');
  const words = query.trim().toLowerCase().split(/\s+/);
  assert.equal(visible().length, data.records.filter(t => words.every(w => t.includes(w))).length);
  assert.ok(visible().length > 0); assert.equal(results.hidden, false);
}
input.value = 'no venue matches this fixture'; input.fire('input');
assert.equal(visible().length, 0); assert.equal(count.textContent, 'No matching places.');
input.value = '<script>alert(1)</script>'; input.fire('input'); assert.equal(visible().length, 0);
input.value = '   '; input.fire('input');
assert.equal(results.hidden, true); assert.equal(count.textContent, ''); assert.equal(location.search, '');
location.search = '?q=Busan'; window.fire('popstate'); assert.equal(input.value, 'Busan'); assert.ok(visible().length > 0);
let prevented = false;
form.fire('submit', {preventDefault() { prevented = true; }}); assert.equal(prevented, true);
'''


def test_image_job_assets_prefer_webp_then_png_and_detail_uses_same_asset(inventory, tmp_path):
    venues, events, _ = inventory
    venue = venues[0]
    png = tmp_path / f'{venue["slug"]}.png'
    webp = tmp_path / f'{venue["slug"]}.webp'
    with patch.object(B, 'VENUE_SKETCH', tmp_path):
        assert 'card-placeholder' in B.venue_media(venue)
        png.write_bytes(b'fixture png')
        assert B.venue_image_path(venue) == png
        assert f'/assets/venue-sketch/{png.name}' in B.venue_card(venue)
        assert 'card-placeholder' not in B.venue_card(venue)
        webp.write_bytes(b'fixture webp')
        assert B.venue_image_path(venue) == webp
        site = B.Site(dt.date(2026, 11, 3), True)
        B.build(site, venues, events, site.today)
        detail = site.pages[f'/finder/{venue["slug"]}/']
        assert f'/assets/venue-sketch/{webp.name}' in detail
        assert f'/assets/venue-sketch/{webp.name}' in B.venue_card(venue)
        assert png.name not in B.venue_media(venue)
        assert 'width="800" height="600"' in B.venue_media(venue)
        assert 'card-placeholder' in B.venue_media(venues[1])
        missing_detail = site.pages[f'/finder/{venues[1]["slug"]}/']
        assert 'detail-media"><div class="card-image card-placeholder"' in missing_detail


def test_release_build_copies_only_selected_published_images(inventory, tmp_path, capsys):
    venues, _, _ = inventory
    sketches = tmp_path / 'image-job'
    sketches.mkdir()
    slug = venues[0]['slug']
    (sketches / f'{slug}.png').write_bytes(b'png fixture')
    (sketches / f'{slug}.webp').write_bytes(b'webp fixture')
    (sketches / 'unpublished-venue.webp').write_bytes(b'not public')
    output = tmp_path / 'release'
    with patch.object(B, 'VENUE_SKETCH', sketches), patch.object(sys, 'argv', [
            'build_site.py', '--release', '--today', '2026-11-03', '--out', str(output)]):
        B.main()
    public = output / 'assets/venue-sketch'
    assert [p.name for p in public.iterdir()] == [f'{slug}.webp']
    assert (public / f'{slug}.webp').read_bytes() == b'webp fixture'
    assert f'/assets/venue-sketch/{slug}.webp' in (output / f'finder/{slug}/index.html').read_text()
    assert 'Allow: /' in (output / 'robots.txt').read_text()
    sitemap = (output / 'sitemap.xml').read_text()
    for key, _ in B.TABS:
        assert f'{B.SITE_URL}/{key}/</loc>' in sitemap
    assert '/ads/' not in sitemap and not (output / 'ads').exists()
    assert (output / '404.html').is_file()
    assert (output / 'google1cc2d472584fa095.html').is_file()
    assert 'leak check OK' in capsys.readouterr().out
