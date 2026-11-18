"""Featured-event lifecycle and cached-page regressions, without maintained data edits."""
from copy import deepcopy
import datetime as dt
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from unittest.mock import patch
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'site'))
import build_site as B

ROOT = Path(__file__).resolve().parents[1]
BEFORE = dt.date(2026, 11, 12)


@pytest.fixture(scope='module')
def inventory():
    venues, events, *_ = B.load(BEFORE, None)
    feature = deepcopy(next(e for e in events if e.get('feature')))
    assert (feature['start'], feature['end']) == (dt.date(2026, 11, 13), dt.date(2026, 11, 15))
    return venues, feature


def render(venues, events, today, vevents=()):
    site = B.Site(today, True)
    B.build(site, venues, events, today, vevents)
    return site


@pytest.mark.parametrize('day,state,label', [
    (12, 'upcoming', 'D-1'), (13, 'ongoing', 'D-DAY'),
    (14, 'ongoing', 'Live now'), (15, 'ongoing', 'Live now'),
    (16, 'ended', 'Ended'), (18, 'ended', 'Ended'),
])
def test_server_lifecycle_and_inclusive_calendar(inventory, day, state, label):
    venues, feature = inventory
    today = dt.date(2026, 11, day)
    site = render(venues, [feature], today)
    detail = site.pages['/hyrox-seoul/']
    assert f'data-event-lifecycle="{state}"' in detail
    assert f'<b data-event-status>{label}</b>' in detail
    assert 'D-0' not in detail
    assert '2026.11.13 (Fri)–2026.11.15 (Sun)' in detail
    assert feature['venue'] in detail and feature['source_label'] in detail
    assert B.SAFETY in detail and B.dot(feature['last_verified']) in detail
    assert 'Official event page</a>' in detail
    for invitation in ('RACE WEEK GUIDE', 'Pick your division', 'Before race day, train here', 'First race?', 'Registration and fees: official page'):
        assert invitation not in detail
    ended = state == 'ended'
    assert ('<title>' + B.E(feature['title']) + (' · Past event' if ended else ' · Event guide')) in detail
    assert ('data-event-ended-only><strong>Past event</strong>' in detail) == ended
    assert ('id="' + feature['slug'] + '"' in site.pages['/events/']) == (not ended)
    calendar = B.ics([feature], today)
    assert calendar.count('BEGIN:VEVENT') == int(not ended)
    assert calendar.startswith('BEGIN:VCALENDAR\r\n')
    assert calendar.endswith('END:VCALENDAR\r\n')
    for route in ('/guide/', '/finder/'):
        assert 'href="/hyrox-seoul/"' in site.pages[route]


@pytest.mark.parametrize('mode', ['empty', 'no-feature', 'missing-flag', 'two-features'])
def test_optional_feature_and_exact_card_link(inventory, mode):
    venues, feature = inventory
    other = dict(feature, slug='other-event', title='Other event', feature=False)
    if mode == 'empty': events = []
    elif mode == 'no-feature': events = [other]
    elif mode == 'missing-flag':
        other.pop('feature')
        events = [other]
    else: events = [feature, dict(other, feature=True)]
    site = render(venues, events, BEFORE)
    has_feature = mode == 'two-features'
    assert ('/hyrox-seoul/' in site.pages) == has_feature
    for page in site.pages.values():
        assert ('href="/hyrox-seoul/"' in page) == (has_feature and '<body class="home">' not in page)
    calendar = site.pages['/events/']
    assert calendar.count('data-event-card') == len(events)
    assert calendar.count('Event guide →') == int(has_feature)
    assert B.ics(events, BEFORE).count('BEGIN:VEVENT') == len(events)
    assert ('data-events-empty hidden' if events else 'data-events-empty>') in calendar


def test_labels_follow_changed_data_and_cross_year_dates(inventory):
    venues, feature = inventory
    feature = dict(feature, title='A Different Event & Season', venue='Different Hall',
                   source_label='Different Official Organizer', facts=[('Format', 'Official reference')],
                   start=dt.date(2032, 12, 31), end=dt.date(2033, 1, 2))
    for today, label in [(dt.date(2032, 12, 30), 'D-1'), (dt.date(2033, 1, 1), 'Live now'),
                         (dt.date(2033, 1, 3), 'Ended')]:
        site = render(venues, [feature], today)
        detail = site.pages['/hyrox-seoul/']
        assert f'<b data-event-status>{label}</b>' in detail
        assert '2032.12.31 (Fri)–2033.1.2 (Sun)' in detail
        assert B.E(feature['title']) in site.pages['/guide/']
        for text in (B.E(feature['title']), feature['venue'], feature['source_label']):
            assert text in detail
        for stale in ('11/13', '11.13', 'KINTEX', 'HYROX Korea'):
            assert stale not in detail


def test_verified_event_feed_is_independent(inventory):
    venues, _ = inventory
    feed = dict(id='independent', title='Independent verified race', category='run', city='Seoul', venue='Park',
                start=BEFORE, end=BEFORE, price_note='', source_url='https://example.org/event', official_url='https://example.org/event', verified_on=BEFORE)
    site = render(venues, [], BEFORE, [feed])
    assert 'id="v-independent"' in site.pages['/events/']
    assert '/hyrox-seoul/' not in site.pages
    assert 'data-events-empty>' in site.pages['/events/']


@pytest.mark.parametrize('mode', ['featured', 'empty', 'no-feature'])
def test_release_routes_sitemap_and_site_locks(inventory, tmp_path, capsys, mode):
    venues, feature = inventory
    events = [] if mode == 'empty' else [dict(feature, feature=mode == 'featured')]
    # Exercise the real release writer and leak checks with in-memory fixtures.
    with patch.object(B, 'load', return_value=(venues, events, [], [])), \
         patch.object(sys, 'argv', ['build_site.py', '--release', '--today', '2026-11-18', '--out', str(tmp_path / 'site')]):
        B.main()
    out = tmp_path / 'site'
    output = capsys.readouterr().out
    assert 'built ' in output and 'leak check OK' in output
    locations = [urlsplit(n.text).path for n in ET.parse(out / 'sitemap.xml').findall('.//{*}loc')]
    assert ('/hyrox-seoul/' in locations) == (mode == 'featured')
    assert (out / 'hyrox-seoul/index.html').exists() == (mode == 'featured')
    assert '/events/' in locations and (out / 'events.ics').is_file()
    assert 'BEGIN:VEVENT' not in (out / 'events.ics').read_text()
    assert '/ads/' not in locations and not (out / 'ads').exists()
    assert (out / '404.html').is_file()
    assert (out / 'google1cc2d472584fa095.html').is_file()
    assert 'Allow: /' in (out / 'robots.txt').read_text()
    for route in locations:
        path = out / route.lstrip('/') / 'index.html'
        assert path.is_file(), route
        page = path.read_text()
        assert f'rel="canonical" href="{B.SITE_URL}{route}"' in page
        assert 'noindex' not in page
    for path in out.rglob('*.html'):
        page = path.read_text()
        for href in re.findall(r'href="(/[^"?#]*)', page):
            if href.startswith('/api/'): continue
            if 'skipping font subsetting' in output and href == '/assets/fonts/Pretendard-Regular.subset.woff2':
                continue  # The builder supports running without optional fonttools.
            destination = out / href.lstrip('/')
            if href.endswith('/'): destination /= 'index.html'
            assert destination.is_file(), (path.relative_to(out), href)
        for asset, version in re.findall(r'(?:src|href)="(/assets/(?:js|css)/[^"?]+)\?v=([a-f0-9]+)"', page):
            assert version == B.asset_ver(asset.removeprefix('/assets/'))
        if '<footer' in page:
            assert ('href="/hyrox-seoul/"' in page) == (mode == 'featured' and '<body class="home">' not in page)


class Tree(HTMLParser):
    """Small DOM fixture built from real emitted markup, shared with Node's vm."""
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, markup):
        super().__init__()
        self.root = dict(tag='document', attrs={}, children=[])
        self.stack = [self.root]
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        node = dict(tag=tag, attrs=dict(attrs), children=[])
        self.stack[-1]['children'].append(node)
        if tag not in self.VOID: self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i]['tag'] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1]['children'].append(data)


@pytest.mark.skipif(not shutil.which('node'), reason='Node is required for cached-page checks')
@pytest.mark.parametrize('timezone', ['UTC', 'America/Los_Angeles', 'Asia/Seoul'])
def test_cached_page_lifecycle(inventory, timezone):
    venues, feature = inventory
    site = render(venues, [feature], BEFORE)
    ended = render(venues, [feature], dt.date(2026, 11, 18))
    payload = dict(script=(ROOT / 'site/static/js/dday.js').read_text(),
                   detail=Tree(site.pages['/hyrox-seoul/']).root,
                   calendar=Tree(site.pages['/events/']).root,
                   ended=Tree(ended.pages['/hyrox-seoul/']).root)
    result = subprocess.run(['node', '-e', JS_CHECKS], input=json.dumps(payload), text=True,
                            capture_output=True, timeout=20, env={**os.environ, 'TZ': timezone})
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'cached lifecycle checks OK' in result.stdout


JS_CHECKS = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
class Element {
  constructor(node) {
    this.tag = node.tag; this.attrs = {...node.attrs}; this.events = {};
    this.children = node.children.map(child => typeof child === 'string' ? child : new Element(child));
    this.children.forEach(child => { if (typeof child !== 'string') child.parentNode = this; });
  }
  hasAttribute(key) { return key in this.attrs; }
  getAttribute(key) { return this.hasAttribute(key) ? this.attrs[key] : null; }
  setAttribute(key, value) { this.attrs[key] = value; }
  get hidden() { return this.hasAttribute('hidden'); }
  set hidden(value) { if (value) this.attrs.hidden = ''; else delete this.attrs.hidden; }
  get textContent() { return this.children.map(c => typeof c === 'string' ? c : c.textContent).join(''); }
  set textContent(value) { this.children = [value]; }
  matches(selector) {
    return selector.split(',').some(selector => {
      const m = selector.trim().match(/^(\w+)?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/);
      if (!m) throw Error('Unhandled selector: ' + selector);
      return (!m[1] || this.tag === m[1]) && (!m[2] ||
        (this.hasAttribute(m[2]) && (m[3] === undefined || this.getAttribute(m[2]) === m[3])));
    });
  }
  querySelectorAll(selector) {
    return this.children.filter(c => typeof c !== 'string').flatMap(c =>
      [...(c.matches(selector) ? [c] : []), ...c.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(event, fn) { (this.events[event] ||= []).push(fn); }
  fire(event) { (this.events[event] || []).forEach(fn => fn()); }
}
function boot(tree, instant = '2026-11-12T14:59:59Z', edit = () => {}) {
  const document = new Element(tree);
  document.visibilityState = 'visible';
  document.title = document.querySelector('title').textContent;
  edit(document);
  const window = new Element({tag:'window', attrs:{}, children:[]});
  const timers = [];
  window.setInterval = (fn, delay) => { assert.ok(delay >= 60000); timers.push(fn); };
  let clock = instant;
  class ClockDate extends Date {
    constructor(...args) { super(...(args.length ? args : [clock])); }
    static now() { return new Date(clock).getTime(); }
  }
  vm.runInNewContext(input.script, {document, window, Date: ClockDate, Intl});
  return {document, window, timers, set(instant) {clock = instant;}};
}
const expectedEnded = new Element(input.ended);
for (const kind of ['detail', 'calendar']) {
  const s = boot(input[kind]);
  const d = s.document;
  const container = d.querySelector('[data-event-lifecycle]');
  const status = container.querySelector('[data-event-status]');
  const dateParent = status.parentNode;
  const dateText = () => dateParent.children.filter(c => c !== status)
    .map(c => typeof c === 'string' ? c : c.textContent).join('');
  const originalDate = dateText();
  const check = (label, state) => {
    assert.equal(status.textContent, label);
    assert.equal(container.getAttribute('data-event-lifecycle'), state);
    assert.equal(dateText(), originalDate, 'Dates and venue must survive countdown updates');
    assert.equal(container.hidden, kind === 'calendar' && state === 'ended');
    if (kind === 'calendar') assert.equal(d.querySelector('[data-events-empty]').hidden, state !== 'ended');
    else {
      assert.equal(d.querySelector('[data-event-ended-only]').hidden, state !== 'ended');
      assert.ok(d.textContent.includes('Official event page'));
      assert.ok(!d.textContent.includes('D-0'));
    }
  };
  check('D-1', 'upcoming');
  // Midnight boundaries are KST, irrespective of the host's time zone.
  for (const [instant, label, state, trigger] of [
    ['2026-11-13T14:59:59Z', 'D-DAY', 'ongoing', 'initial'],
    ['2026-11-12T15:00:00Z', 'D-DAY', 'ongoing', 'pageshow'],
    ['2026-11-13T15:00:00Z', 'Live now', 'ongoing', 'visibilitychange'],
    ['2026-11-14T15:00:00Z', 'Live now', 'ongoing', 'timer'],
    ['2026-11-15T14:59:59Z', 'Live now', 'ongoing', 'pageshow'],
    ['2026-11-15T15:00:00Z', 'Ended', 'ended', 'timer'],
    ['2026-11-18T00:00:00Z', 'Ended', 'ended', 'pageshow'],
  ]) {
    s.set(instant);
    if (trigger === 'initial') {
      const fresh = boot(input[kind], instant);
      assert.equal(fresh.document.querySelector('[data-event-status]').textContent, label);
    }
    if (trigger === 'visibilitychange') d.fire(trigger);
    else if (trigger === 'timer' || trigger === 'initial') s.timers.forEach(fn => fn());
    else s.window.fire(trigger);
    check(label, state);
    s.window.fire('pageshow'); s.timers.forEach(fn => fn());
    check(label, state); // Idempotent updates, including restored pages.
  }
  const late = boot(input[kind], '2026-11-18T00:00:00Z');
  assert.equal(late.document.querySelector('[data-event-status]').textContent, 'Ended');
  if (kind === 'detail') {
    for (const doc of [d, late.document]) {
      assert.equal(doc.title, expectedEnded.querySelector('title').textContent);
      for (const selector of ['meta[name="description"]', 'meta[property="og:title"]', 'meta[property="og:description"]']) {
        assert.equal(doc.querySelector(selector).getAttribute('content'), expectedEnded.querySelector(selector).getAttribute('content'));
      }
      assert.equal(doc.querySelector('h1').textContent, expectedEnded.querySelector('h1').textContent);
    }
  }
}
// Missing status nodes must not cause the date container to be overwritten.
const missing = boot(input.detail, '2026-11-18T00:00:00Z', d => {
  const status = d.querySelector('[data-event-status]');
  status.parentNode.children = status.parentNode.children.filter(c => c !== status);
});
assert.ok(missing.document.textContent.includes('2026.11.13 (Fri)–2026.11.15 (Sun)'));
assert.equal(missing.document.querySelector('[data-event-page]').hidden, false);
assert.equal(missing.document.querySelector('[data-event-ended-only]').hidden, false);
// Invalid dates, reversed ranges and absent attributes leave surrounding content intact.
for (const attrs of [
  {'data-start':'invalid'}, {'data-end':'2026-02-30'}, {'data-end':'2026-01-01'},
  {'data-start':null}, {'data-end':null}
]) {
  let original;
  const invalid = boot(input.detail, '2026-11-18T00:00:00Z', d => {
    const page = d.querySelector('[data-event-page]');
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null) delete page.attrs[key]; else page.attrs[key] = value;
    }
    original = page.textContent;
  });
  invalid.window.fire('pageshow');
  assert.equal(invalid.document.querySelector('[data-event-page]').textContent, original);
}
// An unrelated end-date attribute never makes content an expired calendar card.
const unrelated = boot(input.detail, '2026-11-18T00:00:00Z', d => {
  d.querySelector('h1').setAttribute('data-end', '2026-01-01');
});
assert.equal(unrelated.document.querySelector('h1').hidden, false);
const hidden = boot(input.detail);
hidden.set('2026-11-18T00:00:00Z');
hidden.document.visibilityState = 'hidden'; hidden.document.fire('visibilitychange');
assert.equal(hidden.document.querySelector('[data-event-status]').textContent, 'D-1');
hidden.document.visibilityState = 'visible'; hidden.document.fire('visibilitychange');
assert.equal(hidden.document.querySelector('[data-event-status]').textContent, 'Ended');
console.log('cached lifecycle checks OK');
'''
