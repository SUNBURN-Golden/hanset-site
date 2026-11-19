"""Fixed-date evidence freshness across rendered views; no maintained data changes."""
from copy import deepcopy
import datetime as dt
from pathlib import Path
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'site'))
import build_site as B

TODAY = dt.date(2032, 1, 1)
AGES = [90, 91, 0, -1]
TATTOO_GUIDE = '/guides/tattoo-friendly-saunas-korea/'
NON_GUEST_GUIDE = '/guides/hotel-saunas-korea-non-guests/'
DROP_IN_GUIDE = '/guides/crossfit-hyrox-drop-in-korea/'
GUIDE_FIELDS = (
    (TATTOO_GUIDE, 'Tattoo rule', 0, 'tattoo', 'Cover tattoos.'),
    (NON_GUEST_GUIDE, 'Open to visitors?', 0, 'dropin', 'Hotel guests only.'),
    (DROP_IN_GUIDE, 'Drop-in', 1, 'dropin', 'Book ahead.'),
    (DROP_IN_GUIDE, 'English', 1, 'english', 'English-speaking coach.'),
)


@pytest.fixture
def inventory(monkeypatch):
    # Reuse the renderer's venue shape, replacing all published fixture evidence.
    template = next(v for v in B.load(TODAY)[0] if v['slug'] == 'orakai-cheonggyesan')
    v = deepcopy(template)
    v.update(slug='freshness-sauna', title='Fixture Sauna', source='https://example.org/venue',
             last_verified=TODAY, stale=True, hyrox=True, c={"kind": "호텔 사우나"}, extra_sources=[],
             facts=[('Price', 'Adult ₩24,000'), ('Hours', 'Daily 06:00–22:00'),
                    ('Facilities', 'Dry sauna and cold bath 12°C')],
             price='₩24,000', price_term='Adult ₩24,000', price_checked=TODAY,
             price_label='Adult', price_caveat='Check before you go.',
             tips=['Bring swimwear.'], unverified=['Future booking not confirmed.'],
             off={'date': '', 'cf': '', 'hyrox': ''})
    box = deepcopy(v)
    box.update(slug='freshness-box', title='Fixture Box', kind_key='box', kind='CrossFit Box', sub='box')
    event = dict(slug='fixture-race', title='Fixture Race', kind='HYROX', city='Seoul', venue='Fixture Hall',
                 start=TODAY + dt.timedelta(days=5), end=TODAY + dt.timedelta(days=7),
                 facts=[('Date', 'Organizer confirmed')], note='Check fees before booking.',
                 url='https://example.org/race', source_label='Official organizer', last_verified=TODAY, feature=True)
    vevent = dict(id='fixture-feed', title='Fixture Feed Race', category='run', city='Seoul', venue='Fixture Park',
                  start=event['start'], end=event['end'], price_note='See official fees',
                  source_url='https://example.org/evidence', official_url='https://example.org/registration', verified_on=TODAY)
    creator = dict(id='fixture-creator', display_name='Fixture Creator', handle='@fixture', platform='other',
                   focus=['run'], city='Seoul', profile_url='https://example.org/profile', verified_on=TODAY)
    venues = [v, box]
    visitors = {venue['slug']: {} for venue in venues}
    for _, _, idx, field, value in GUIDE_FIELDS:
        visitors[venues[idx]['slug']][field] = value
    monkeypatch.setattr(B, 'FOREIGNER', visitors)
    monkeypatch.setattr(B, 'CLOSED_ALL', {})
    monkeypatch.setattr(B, 'CLOSED_MONTH', {})
    return venues, [event], vevent, creator


def render(inventory, today=TODAY):
    venues, events, feed, creator = inventory
    site = B.Site(today, True)
    B.build(site, venues, events, today, [feed], [creator])
    return site.pages


def matching(markup, pattern, token):
    return next(item for item in re.findall(pattern, markup, re.S) if token in item)


def row(markup, token):
    return matching(markup, r'<tr>.*?</tr>', token)


def article(markup, token):
    return matching(markup, r'<article\b.*?</article>', token)


def assert_evidence(markup, checked, age, source):
    assert (B.OUTDATED in markup) == (age > 90)
    assert checked in markup
    assert source in markup


def guide_table(pages, path, heading):
    return matching(pages[path], r'<table class="facts">.*?</table>(?:<p class="muted">.*?</p>)?',
                    f'<th>{heading}</th>')


def assert_pending(table, count):
    note = table.split('</table>', 1)[1]
    if count:
        noun = 'venue' if count == 1 else 'venues'
        assert re.findall(r'\d+ venues? pending official confirmation \(확인 중\)', note) == [
            f'{count} {noun} pending official confirmation (확인 중)'
        ]
        assert '<a href="/finder/">Finder</a>' in note
    else:
        assert note == ''


def assert_guide_safety(pages):
    safety = f'<p class="safety">{B.SAFETY}</p>'
    for path, heading in ((TATTOO_GUIDE, 'Tattoo rule'), (NON_GUEST_GUIDE, 'Open to visitors?')):
        assert pages[path].count(safety) == 1
        assert guide_table(pages, path, heading) + safety in pages[path]


@pytest.mark.parametrize('explicit', [False, True], ids=['venue-date', 'field-date'])
@pytest.mark.parametrize('age', [0, 90, 91])
def test_official_visitor_guide_rows_keep_their_source_and_freshness(inventory, explicit, age):
    venues = inventory[0]
    checked = TODAY - dt.timedelta(days=age)
    for v in venues:
        v['last_verified'] = TODAY - dt.timedelta(days=0 if age == 91 else 91) if explicit else checked
    if explicit:
        for _, _, idx, field, value in GUIDE_FIELDS:
            B.FOREIGNER[venues[idx]['slug']][field] = f'{value} (official page, checked {checked.isoformat()})'
    pages = render(inventory)
    for path, heading, idx, field, _ in GUIDE_FIELDS:
        v = venues[idx]
        table = guide_table(pages, path, heading)
        entry = row(table, f'href="/finder/{v["slug"]}/"')
        assert f'<td>{B.E(B.FOREIGNER[v["slug"]][field])}</td>' in entry
        assert_evidence(entry, f'Checked {checked.isoformat() if explicit else B.dot(checked)}',
                        age, f'<a href="{v["source"]}" rel="noopener noreferrer">Official source · ')
        assert_pending(table, 0)
    assert_guide_safety(pages)


@pytest.mark.parametrize(('value', 'venue_evidence'), [
    pytest.param(None, {}, id='missing-field'),
    pytest.param(' \t ', {}, id='blank-field'),
    pytest.param(B.UNKNOWN, {}, id='unknown-placeholder'),
    pytest.param('Unknown', {}, id='unknown-value'),
    pytest.param('확인 중', {}, id='korean-placeholder'),
    pytest.param('Fixture policy (source claim).', {}, id='source-claim'),
    pytest.param('Fixture policy.', {'confidence': 'partial'}, id='unofficial-venue-source'),
    pytest.param('Fixture policy (venue reply 2032-01-01).', {}, id='venue-reply'),
    pytest.param('Fixture policy (hotel reply 2032-01-01).', {}, id='hotel-reply'),
    pytest.param('Fixture policy (official page and hotel reply, checked 2032-01-01).', {}, id='mixed-citation'),
    pytest.param('Fixture policy — official page and hotel reply, checked 2032-01-01.', {}, id='mixed-inline-citation'),
    pytest.param('Fixture policy.', {'source': '/missing-host'}, id='invalid-source-url'),
    pytest.param('Fixture policy.', {'last_verified': None}, id='missing-venue-date'),
    pytest.param('Fixture policy.', {'last_verified': TODAY + dt.timedelta(days=1)}, id='future-venue-date'),
    pytest.param('Fixture policy (official page, checked).', {}, id='missing-explicit-date'),
    pytest.param('Fixture policy (official page, checked 2032-02-30).', {}, id='invalid-explicit-date'),
    pytest.param('Fixture policy (official page, checked 1/1).', {}, id='yearless-explicit-date'),
    pytest.param('Fixture policy (official page, checked 2031-12-31 or 2032-01-01).', {}, id='ambiguous-explicit-date'),
    pytest.param('Fixture policy (official page, checked 2032-01-02).', {}, id='future-explicit-date'),
])
def test_unsupported_visitor_fields_are_pending_in_each_table(inventory, value, venue_evidence):
    venues = inventory[0]
    for v in venues:
        v.update(venue_evidence)
    for _, _, idx, field, _ in GUIDE_FIELDS:
        fields = B.FOREIGNER[venues[idx]['slug']]
        if value is None:
            del fields[field]
        else:
            fields[field] = value
    pages = render(inventory)
    for path, heading, _, _, _ in GUIDE_FIELDS:
        table = guide_table(pages, path, heading)
        assert '<tbody></tbody>' in table
        assert 'Fixture policy' not in pages[path]
        assert_pending(table, 1)
    assert_guide_safety(pages)


def test_drop_in_and_english_support_count_pending_independently(inventory):
    venues = inventory[0]
    box = venues[1]
    B.FOREIGNER[box['slug']]['english'] = 'English available (source claim).'
    venues.append(dict(box, slug='second-fixture-box', title='Second Fixture Box'))
    pages = render(inventory)
    drop_in = guide_table(pages, DROP_IN_GUIDE, 'Drop-in')
    english = guide_table(pages, DROP_IN_GUIDE, 'English')
    assert 'Book ahead.' in row(drop_in, f'/finder/{box["slug"]}/')
    assert '<tbody></tbody>' in english
    assert 'English available' not in pages[DROP_IN_GUIDE]
    assert_pending(drop_in, 1)
    assert_pending(english, 2)


@pytest.mark.parametrize('age', AGES)
def test_venue_cards_detail_groups_and_checklists(inventory, age):
    venues, *_ = inventory
    checked = TODAY - dt.timedelta(days=age)
    for v in venues:
        v['last_verified'] = v['price_checked'] = checked
        v['stale'] = age <= 90  # a cached flag must never control a later render
    before = deepcopy(inventory)
    pages = render(inventory)
    v = venues[0]
    # Finder, category and HYROX mini-cards share visible, untruncated metadata.
    for path in ('/finder/', '/sauna/', '/hyrox-seoul/'):
        card = article(pages[path], '/finder/freshness-sauna/')
        assert_evidence(card, B.dot(checked), age, 'https://example.org/venue')
        assert 'card-evidence' in card
        assert card.index('card-evidence') > card.index('</p>', card.index('card-meta'))
        assert '₩24,000' in card and 'Dry sauna' in card
    detail = pages['/finder/freshness-sauna/']
    assert_evidence(B.v_badges(v, TODAY), 'Sauna', age, 'badge')
    assert ('Last checked over 90 days ago' in detail) == (age > 90)
    for label in ('Price', 'Hours', 'Facilities', 'Tattoos', 'Drop-in for visitors'):
        assert_evidence(row(detail, f'<th>{label}</th>'), B.dot(checked), age, 'https://example.org/venue')
    for markup in (B.price_html(v, TODAY), B.venue_facts(v, TODAY), B.fchk(v, TODAY),
                   matching(detail, r'<li>.*?</li>', 'Bring swimwear.')):
        assert_evidence(markup, B.dot(checked), age, 'https://example.org/venue')
    glance = detail.split('class="glance-grid"', 1)[1].split('<h2>Source notes', 1)[0]
    for label in ('Adult ₩24,000', 'Daily 06:00–22:00', 'Dry sauna and cold bath'):
        fact = matching(glance, r'<span class="fact-line">.*?</small></span>', label)
        assert_evidence(fact, B.dot(checked), age, 'https://example.org/venue')
    assert inventory == before


@pytest.mark.parametrize('age', AGES)
def test_legacy_feature_verified_event_creator_and_calendar(inventory, age):
    _, events, feed, creator = inventory
    checked = TODAY - dt.timedelta(days=age)
    events[0]['last_verified'] = feed['verified_on'] = creator['verified_on'] = checked
    before = deepcopy(inventory)
    pages = render(inventory)
    assert_evidence(article(pages['/events/'], 'id="fixture-race"'), B.dot(checked), age, events[0]['url'])
    feature = matching(pages['/hyrox-seoul/'], r'<p class="muted">.*?</p>', events[0]['source_label'])
    assert_evidence(feature, B.dot(checked), age, events[0]['url'])
    feed_card = article(pages['/events/'], 'id="v-fixture-feed"')
    assert_evidence(feed_card, B.dot(checked), age, feed['source_url'])
    assert feed['official_url'] in feed_card
    creator_card = article(pages['/creators/'], 'id="c-fixture-creator"')
    assert_evidence(creator_card, B.dot(checked), age, creator['profile_url'])
    calendar = B.ics(events, TODAY).replace('\r\n ', '')
    description = next(line for line in calendar.splitlines() if line.startswith('DESCRIPTION:'))
    assert_evidence(description, B.dot(checked), age, events[0]['source_label'])
    assert f'URL:{events[0]["url"]}' in calendar
    assert inventory == before


@pytest.mark.parametrize('age', AGES)
@pytest.mark.parametrize('venue_age', [0, 91])
def test_closure_evidence_is_independent_in_detail_guide_and_month(inventory, monkeypatch, age, venue_age):
    v = inventory[0][0]
    v['last_verified'] = TODAY - dt.timedelta(days=venue_age)
    checked = TODAY - dt.timedelta(days=age)
    closure = dict(closed='First Monday', checked=checked.isoformat(), source='Official closure notice',
                   url='https://example.org/closure', until=(TODAY + dt.timedelta(days=12)).isoformat())
    monkeypatch.setattr(B, 'CLOSED_ALL', {v['slug']: closure})
    monkeypatch.setattr(B, 'CLOSED_MONTH', dict(month='2032-01', title='January closed days',
                        items=[dict(date='2032-01-05', slugs=[v['slug']])]))
    pages = render(inventory)
    detail_row = row(pages['/finder/freshness-sauna/'], '<th>Closed on</th>')
    guide = pages['/guides/closed-days-korea/']
    guide_row = row(guide, '/finder/freshness-sauna/')
    month_entry = matching(guide, r'<span class="closure-entry">.*?</span>', v['title'])
    for markup in (detail_row, guide_row, month_entry):
        assert_evidence(markup, B.dot(checked), age, closure['url'])
        assert closure['source'] in markup
    assert '2032.01.05' not in month_entry  # occurrence is not a check date


@pytest.mark.parametrize('age', AGES)
@pytest.mark.parametrize('citation', ['(venue reply {date})', '— official page, checked {date}'])
def test_independent_citations_on_detail_tips_and_checklist(inventory, age, citation):
    v = inventory[0][0]
    v['last_verified'] = TODAY - dt.timedelta(days=91 if age <= 90 else 0)
    checked = TODAY - dt.timedelta(days=age)
    text = 'Cover tattoos. ' + citation.format(date=checked.isoformat())
    v['facts'] = [('Tattoos', text)]
    v['tips'] = [text]
    B.FOREIGNER[v['slug']]['tattoo'] = text
    pages = render(inventory)
    for markup in (row(pages['/finder/freshness-sauna/'], '<th>Tattoos</th>'),
                   matching(pages['/finder/freshness-sauna/'], r'<li>.*?</li>', 'Cover tattoos.'),
                   row(B.fchk(v, TODAY), '<th>Tattoos</th>')):
        assert_evidence(markup, checked.isoformat(), age, 'Venue reply' if 'reply' in citation else v['source'])
        assert ('Source claim' in markup) == ('reply' in citation)
        assert ('Official source' in markup) == ('reply' not in citation)
        if 'reply' in citation:
            assert v['source'] not in markup


@pytest.mark.parametrize('age', AGES)
def test_official_affiliation_uses_its_own_date_and_link(inventory, age):
    v = inventory[0][1]
    v['last_verified'] = TODAY - dt.timedelta(days=91 if age <= 90 else 0)
    checked = TODAY - dt.timedelta(days=age)
    v['off'] = dict(date=checked.isoformat(), cf='https://example.org/affiliate', hyrox='https://example.org/gym-list')
    page = render(inventory)['/finder/freshness-box/']
    for markup, url in zip(re.findall(r'<div class="off">.*?</div>', page), (v['off']['cf'], v['off']['hyrox'])):
        assert_evidence(markup, checked.isoformat(), age, url)
    assert page.count('<div class="off">') == 2


def test_injected_far_future_and_cross_year_dates_control_events(inventory):
    _, events, feed, _ = inventory
    first = dt.date(2040, 12, 31)
    events[0].update(start=dt.date(2041, 1, 1), end=dt.date(2041, 1, 2))
    feed.update(start=events[0]['start'], end=events[0]['end'])
    ended = dict(events[0], slug='ended', feature=False, start=first - dt.timedelta(days=2), end=first - dt.timedelta(days=1))
    events.append(ended)
    for today in (first, dt.date(2041, 1, 1), dt.date(2041, 1, 2), dt.date(2041, 1, 3), first):
        pages = render(inventory, today)
        html = pages['/events/']
        calendar = B.ics(events, today).replace('\r\n ', '')
        expected = {e['slug'] for e in events if e['end'] >= today}
        assert set(re.findall(r'UID:([^@]+)@hanset', calendar)) == expected
        assert {e['slug'] for e in events if f'id="{e["slug"]}"' in html} == expected
        month = re.search(r'<span class="vmon">(.*?)</span>', html)[1]
        assert month == ('Jan 2041' if today.year == 2040 else 'Jan')


def test_calendar_escaping_folding_and_future_check_date(inventory):
    event = inventory[1][0]
    event.update(title='Race, fun; test\\name\nNext', source_label='Official, organizer; notice\\source\nNext',
                 last_verified=TODAY + dt.timedelta(days=1))
    calendar = B.ics([event], TODAY)
    assert all(len(line.encode('utf-8')) <= 75 for line in calendar.split('\r\n'))
    unfolded = calendar.replace('\r\n ', '')
    assert 'SUMMARY:Race\\, fun\\; test\\\\name\\nNext' in unfolded
    assert 'Official\\, organizer\\; notice\\\\source\\nNext' in unfolded
    assert B.dot(event['last_verified']) in unfolded and B.OUTDATED not in unfolded


@pytest.mark.parametrize('age', AGES)
def test_source_claim_links_and_unknown_classifications_survive_age(inventory, age):
    v = inventory[0][0]
    checked = TODAY - dt.timedelta(days=age)
    v.update(last_verified=checked, confidence='partial', status='partial',
             unverified=['Entry fee', f'Access pending (hotel reply {checked.isoformat()})'])
    pages = render(inventory)
    finder_card = article(pages['/finder/'], '/finder/freshness-sauna/')
    assert_evidence(finder_card, B.dot(checked), age, f'<a href="{v["source"]}"')
    assert 'Source claim' in finder_card and 'Official source' not in finder_card
    detail = pages['/finder/freshness-sauna/']
    unconfirmed = detail.split('<h2>Unconfirmed</h2>', 1)[1].split('</ul>', 1)[0]
    assert 'Official source' not in unconfirmed and 'Source claim' not in unconfirmed
    for token, date in (('Entry fee', B.dot(checked)), ('Access pending', checked.isoformat())):
        item = matching(unconfirmed, r'<li>.*?</li>', token)
        assert_evidence(item, date, age, 'Unconfirmed')
    unknown = B.fact_lines([('Facilities', B.UNKNOWN)], v, TODAY)
    assert B.UNKNOWN in unknown and 'Official source' not in unknown and 'fact-source' not in unknown


@pytest.mark.parametrize('raw', ['', None, '10/17', '2032-02-30', '2031-10-02 or 2032-01-01'])
def test_missing_or_ambiguous_closure_and_affiliation_dates_are_unconfirmed(inventory, raw):
    v = inventory[0][0]
    v['last_verified'] = TODAY - dt.timedelta(days=91)
    closure = dict(closed='First Monday', checked=raw, source='Official closure notice', url='https://example.org/closure')
    v['off'] = dict(date=raw, cf='https://example.org/affiliate', hyrox='')
    for markup in (B.closure_provenance(closure, TODAY), B.off_html(v, TODAY)):
        assert 'date not confirmed' in markup and B.OUTDATED not in markup
        assert 'https://example.org/' in markup
        if raw: assert raw in markup
        assert B.dot(v['last_verified']) not in markup
