"""Evidence boundaries: no venue-level status upgrade, invented facilities or badges."""
from copy import deepcopy
import datetime as dt
import json
from pathlib import Path
from shutil import copyfile
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'site'))
import build_site as B


@pytest.fixture(scope='module')
def venues():
    rows, *_ = B.load(dt.date(2026, 11, 3))
    return {v['slug']: v for v in rows}


@pytest.mark.parametrize('value,status', [
    ('Dry sauna and cold bath 12°C', 'fact'),
    ('Dry sauna not confirmed', 'unverified'),
    ('Dry sauna — unverified clue', 'unverified'),
    ('Dry sauna — third-party roundup', 'source_claim'),
    ('Dry sauna — Source claim', 'source_claim'),
    ('Dry sauna — venue reply, 2026-10-18', 'source_claim'),
    ('Unconfirmed', 'unverified'),
])
def test_status_depends_on_each_fact(venues, value, status):
    v = deepcopy(venues['orakai-cheonggyesan'])
    v['status'] = 'verified'
    assert B.evidence_status(v, value) == status
    note = B.fact_provenance(v, value, dt.date(2026, 11, 3))
    assert {'fact': 'Official source', 'unverified': 'Unconfirmed', 'source_claim': 'Source claim'}[status] in note
    if status != 'fact':
        assert 'Official source' not in note
        assert 'href=' not in note


@pytest.mark.parametrize('value', ['', B.UNKNOWN, B.UNKNOWN + ' (temperature)', 'pending', 'not confirmed', 'not yet announced'])
def test_unknown_has_no_source_provenance(venues, value):
    assert B.fact_provenance(venues['orakai-cheonggyesan'], value, dt.date(2026, 11, 3)) == ''


@pytest.mark.parametrize('confidence,expected', [
    ('verified', 'fact'),
    ('high (official page read)', 'fact'),
    ('partial', 'source_claim'),
    ('unverified', 'source_claim'),
    ('', 'source_claim'),
    ('high', 'source_claim'),
    ('high (not official)', 'source_claim'),
    ('high (public licence record only; venue official page unreachable)', 'source_claim'),
    ('high (third-party roundup; official page unavailable)', 'source_claim'),
])
@pytest.mark.parametrize('value', ['급냉탕', 'Cold bath 12°C', 'Dry sauna, wet sauna, private sauna'])
def test_verification_metadata_gates_all_amenity_badges(venues, confidence, expected, value):
    v = deepcopy(venues['orakai-cheonggyesan'])
    v['status'] = 'verified'
    v['confidence'] = confidence
    v['facts'] = [('Facilities', value)]
    assert B.evidence_status(v, value) == expected
    assert bool(B.amenity_labels(v)) == (expected == 'fact')
    note = B.fact_provenance(v, value, dt.date(2026, 11, 3))
    assert ('Official source' in note) == (expected == 'fact')
    assert ('href=' in note) == (expected == 'fact')


@pytest.mark.parametrize('status', [None, '', 'partial', 'unverified'])
def test_missing_verification_provenance_does_not_default_to_fact(venues, status):
    v = deepcopy(venues['orakai-cheonggyesan'])
    del v['confidence']
    if status is None:
        del v['status']
    else:
        v['status'] = status
    v['facts'] = [('Facilities', '급냉탕')]
    assert B.evidence_status(v, '급냉탕') == 'source_claim'
    assert B.amenity_labels(v) == []
    v['source'] = ''
    assert B.evidence_status(v, '급냉탕') == 'unverified'
    assert 'href=' not in B.fact_provenance(v, '급냉탕', dt.date(2026, 11, 3))


def test_directory_and_roundup_never_gain_official_fact_status(venues):
    for slug in ('basalt-crossfit', 'seian-yeonnam-seoul'):
        v = deepcopy(venues[slug])
        v['status'] = 'verified'
        v['facts'] = [('Facilities', 'Dry sauna, wet sauna, cold bath 10°C, private sauna, showers, parking')]
        assert B.evidence_status(v, v['facts'][0][1]) == 'source_claim'
        assert not B.amenity_labels(v)


@pytest.mark.parametrize('value,expected', [
    ('Dry sauna, wet sauna, cold bath 12°C, private sauna', ['Great cold plunge', 'Dry sauna', 'Wet/Steam', 'Private']),
    ('Dry sauna, wet sauna, cold bath 16°C', ['Dry sauna', 'Wet/Steam']),
    ('Cold bath 10–20°C', []),
    ('Cold bath 10–15°C', ['Great cold plunge']),
    ('Cold bath temperature: 15°C', ['Great cold plunge']),
    ('Sauna 10°C, cold bath 20°C', []),
    ('Sauna 80°C, cold shower 14°C', []),
    ('Cold baths, ice room', []),
    ('급냉탕', ['Great cold plunge']),
    ('No dry sauna or wet sauna', []),
])
def test_sauna_badges_require_specific_positive_evidence(venues, value, expected):
    v = deepcopy(venues['orakai-cheonggyesan'])
    v['facts'] = [('Facilities', value)]
    assert B.amenity_labels(v) == expected
    for suffix in (' — unconfirmed', ' — not confirmed', ' — pending', ' — Source claim', ' — third-party roundup'):
        v['facts'] = [('Facilities', value + suffix)]
        assert B.amenity_labels(v) == []
    v['facts'] = [('Facilities', value)]
    assert B.amenity_badges(v).count('class="badge ') == len(expected)
    assert B.amenity_badges(v, limit=3).count('class="badge ') == min(3, len(expected))


def test_box_badges_distinguish_english_coach_from_english_page(venues):
    v = deepcopy(venues['crossfit-hannam'])
    v['facts'] = [('Facilities', 'Showers and parking'), ('Drop-in', 'Booking available'), ('Language', 'English-speaking coach')]
    assert B.amenity_labels(v) == ['Showers', 'Parking', 'Drop-in OK', 'English coach']
    assert B.amenity_badges(v, limit=3).count('class="badge ') == 3
    assert B.amenity_badges(v).count('class="badge ') == 4
    v['facts'] = [('Language', 'Official English guide'), ('Facilities', 'No showers or parking')]
    assert B.amenity_labels(v) == []


def test_missing_temperature_and_plunge_are_not_inferred_from_cold_shower(venues):
    v = venues['sisu-house']
    rows = dict(dict(B.fact_groups(v))['Facilities & temperatures'])
    assert rows['Cold bath temperature'] == B.UNKNOWN
    assert rows['Cold plunge'] == B.UNKNOWN
    assert 'Great cold plunge' not in B.amenity_labels(v)
    assert B.UNKNOWN in B.fact_lines([('Sauna temperature', B.UNKNOWN)], v, dt.date(2026, 11, 3))


def test_rich_groups_include_original_facilities_and_programs(venues):
    for v in venues.values():
        groups = dict(B.fact_groups(v))
        assert 'Facilities & temperatures' in groups and 'Programs' in groups
        for label, value in v['facts']:
            if label in ('Facilities', 'Equipment (official)'):
                assert (label, value) in groups['Facilities & temperatures']
    holis = venues['holis-training-club']
    assert 'Group session' in B.price_html(holis, dt.date(2026, 11, 3))
    assert 'Day pass ₩30,000' in B.price_html(holis, dt.date(2026, 11, 3))
    assert '₩40,000' in B.price_short(holis)
    assert 'Day pass' not in B.price_short(holis)


def test_third_party_price_and_hour_clues_are_redacted_everywhere(venues):
    for v in venues.values():
        for key, value in v['facts']:
            if any(word in value.lower() for word in ('unverified clue', 'third-party', 'blog')) and (
                    key.lower().startswith(('price', 'hours')) or '₩' in value):
                shown = B.display_value(v, key, value)
                assert 'Unconfirmed' in shown and '₩' not in shown
                assert value not in B.fact_lines([(key, value)], v, dt.date(2026, 11, 3))
                assert 'Unconfirmed' in B.fact_provenance(v, shown, dt.date(2026, 11, 3))


def test_warning_prose_survives_but_age_uses_evidence_date(venues):
    v = deepcopy(venues['orakai-cheonggyesan'])
    today = dt.date(2026, 11, 3)
    v['last_verified'] = today - dt.timedelta(days=91)
    v['stale'] = False  # render date takes precedence over a cached load flag
    assert B.OUTDATED in B.fact_provenance(v, 'Dry sauna', today)
    v['last_verified'] = today
    warning = 'Dry sauna (older than 90 days)'
    assert B.OUTDATED not in B.fact_provenance(v, warning, today)
    assert warning in B.fact_lines([('Facilities', warning)], v, today)


@pytest.mark.parametrize('citation,checked_text', [
    ('(venue reply 10/17)', '10/17'),
    ('(hotel reply)', 'date not confirmed'),
    ('— venue reply (inquiry sent 2026-10-18)', 'date not confirmed'),
    ('(venue reply; inquiry sent 2026-10-18)', 'date not confirmed'),
    ('(official page, checked 10/17)', '10/17'),
    ('(official page, checked date pending)', 'date not confirmed'),
    ('(venue reply 2026-01-01 or 2026-10-18)', '2026-01-01, 2026-10-18'),
    ('(official page checked 2026-01-01; checked 2026-10-18)', '2026-01-01, 2026-10-18'),
    ('(venue reply 2026-02-30)', '2026-02-30'),
])
def test_unknown_or_ambiguous_citation_dates_never_inherit_venue_age(venues, citation, checked_text):
    v = deepcopy(venues['orakai-cheonggyesan'])
    v['last_verified'] = dt.date(2020, 1, 1)
    value = 'Closed 2026-10-28 ' + citation
    note = B.fact_provenance(v, value, dt.date(2026, 11, 3))
    assert checked_text in note
    assert 'date not confirmed' in note
    assert '2026-10-28' not in note and '2020.01.01' not in note
    assert B.OUTDATED not in note
    assert B.E(value) in B.fact_lines([('Closed', value)], v, dt.date(2026, 11, 3))


@pytest.mark.parametrize('date_text', ['2026-10-18', '2026.10.18', 'Oct 18, 2026', 'October 18, 2026'])
@pytest.mark.parametrize('marker', ['(venue reply {date})', '(official page, checked {date})'])
def test_unambiguous_citation_formats_use_only_the_evidence_date(venues, date_text, marker):
    v = deepcopy(venues['orakai-cheonggyesan'])
    v['last_verified'] = dt.date(2026, 1, 1)
    value = 'Closed 2026-10-28 ' + marker.format(date=date_text)
    checked = dt.date(2026, 10, 18)
    for age in (90, 91, 0, -1):
        note = B.fact_provenance(v, value, checked + dt.timedelta(days=age))
        assert date_text in note and '2026-10-28' not in note
        assert (B.OUTDATED in note) == (age > 90)
        assert ('Venue reply' in note) == ('reply' in marker)
        assert ('href=' in note) == ('reply' not in marker)


@pytest.mark.parametrize('value', [
    'Closed 2026-10-28 (inquiry checked 2026-01-01; awaiting official hotel reply)',
    'Closed 2026-10-28 — venue reply; inquiry checked 2026-01-01',
])
def test_inquiry_check_dates_do_not_date_the_evidence(venues, value):
    v = deepcopy(venues['orakai-cheonggyesan'])
    v['last_verified'] = dt.date(2020, 1, 1)
    note = B.fact_provenance(v, value, dt.date(2026, 11, 3))
    assert '2026-01-01' not in note and '2026-10-28' not in note
    assert 'date not confirmed' in note and B.OUTDATED not in note


def numeric_price_part(v, today):
    # The headline number + its own provenance, before the full terms block.
    return B.price_html(v, today).split('<div class="price price-context">')[0]


def test_reply_derived_price_stays_pending_on_card_and_headline(venues):
    today = dt.date(2026, 11, 3)
    sisu = venues['sisu-house']
    html = B.price_html(sisu, today)
    card = B.venue_card(sisu, today)
    assert numeric_price_part(sisu, today) == ''
    assert 'data-price="0"' in card and '₩79,000' not in card
    assert '₩79,000' in html and 'Official source' not in html
    assert 'Venue reply · 2026-10-18' in html and 'Source claim' in html


def test_official_numeric_price_keeps_provenance_beside_a_reply_discount(venues):
    # paradise's headline ₩55,000 is the official adult rate; only the sibling
    # guest-discount term is a reply. Amount scoping must keep the official hint.
    today = dt.date(2026, 11, 3)
    paradise = venues['paradise-hotel-busan']
    html = B.price_html(paradise, today)
    numeric = numeric_price_part(paradise, today)
    assert '₩55,000' in numeric and 'Official source' in numeric
    assert 'Checked 2026.10.18' in numeric
    assert f'href="{B.E(paradise["source"])}"' in numeric
    card = B.venue_card(paradise, today)
    assert 'data-price="1"' in card and '₩55,000' in card
    assert 'Venue reply · 2026-10-17' in html
    discount = next(val for key, val in paradise['facts'] if key == 'Guest discount rate')
    assert 'Official source' not in B.fact_provenance(paradise, discount, today)


def test_unmatched_price_stays_pending_and_keeps_its_dated_claim(venues):
    today = dt.date(2026, 11, 3)
    repo = venues['solo-sauna-repo-noryangjin']
    html = B.price_html(repo, today)
    card = B.venue_card(repo, today)
    assert numeric_price_part(repo, today) == ''
    assert 'data-price="0"' in card and '₩45,000' not in card
    assert '₩45,000' in html and 'Source claim' in html
    assert 'Checked Oct 22, 2026' in html and B.dot(repo['last_verified']) not in html


def test_signiel_guest_price_has_its_own_provenance(venues):
    today = dt.date(2026, 11, 3)
    signiel = venues['signiel-seoul']
    numeric = numeric_price_part(signiel, today)
    assert '₩44,000' in numeric and 'Official source' in numeric
    assert 'Membership rates not listed' in B.price_html(signiel, today)


@pytest.fixture
def load_price_case(tmp_path, monkeypatch, venues):
    def load(slug, terms, today, **csv_fields):
        venue = venues[slug]
        content = tmp_path / 'content'
        content.mkdir()
        for filename in ('ads.json', 'venues.json', 'events.json', 'events_en.json'):
            copyfile(B.ROOT / 'content' / filename, content / filename)
        english = json.loads((B.ROOT / 'content/venues_en.json').read_text(encoding='utf-8'))
        english[slug]['facts'][:len(terms)] = terms
        (content / 'venues_en.json').write_text(json.dumps(english), encoding='utf-8')
        original_read = B.read_csv

        def read_csv(filename):
            rows = original_read(filename)
            if filename == venue['c']['csv'] + '.csv':
                row = next(row for row in rows if row['name'] == venue['name'])
                row.update(csv_fields)
            return rows

        with monkeypatch.context() as patch:
            patch.setattr(B, 'ROOT', tmp_path)
            patch.setattr(B, 'read_csv', read_csv)
            return next(v for v in B.load(today)[0] if v['slug'] == slug)

    return load


@pytest.mark.parametrize('age', [90, 91])
def test_headline_date_and_evidence_are_independent_of_an_equal_reply_rate(load_price_case, age):
    today = dt.date(2026, 11, 3)
    checked = today - dt.timedelta(days=age)
    v = load_price_case('grand-josun-busan', [
        ('Price', f'Adult {{price}} (tax incl.) — official page, checked {checked}'),
        ('Guest discount rate', f'₩55,000 — venue reply, {today}'),
    ], today)
    numeric = numeric_price_part(v, today)
    assert v['price_checked'] == checked
    assert '₩55,000' in numeric and 'Official source' in numeric
    assert f'Checked {checked}' in numeric and B.dot(v['last_verified']) not in numeric
    assert (B.OUTDATED in numeric) == (age > 90)
    assert f'Venue reply · {today} · Source claim' in B.price_html(v, today)
    assert 'data-price="1"' in B.venue_card(v, today)


@pytest.mark.parametrize('terms,csv_fields', [
    ([('Price', 'Adult ₩55,000')], {}),
    ([('Price', 'Adult {price}'), ('Day pass', '{price}')], {}),
    ([('Booking', 'Adult {price}')], {}),
    ([('Price', 'Adult {price} — venue reply, 2026-10-18'), ('Day pass', '₩55,000')], {}),
    ([('Price', 'Adult {price} (official page and hotel reply 2026-10-18)')], {}),
    ([('Price', 'Adult {price} — third-party roundup')], {}),
    ([('Price', 'Adult {price}')], {'source_url': 'https://blog.example.org/rates'}),
    ([('Price', 'Adult {price} — official page, checked')], {}),
    ([('Price', 'Adult {price} — official page, checked 10/18')], {}),
    ([('Price', 'Adult {price} — official page, checked 2026-10-18 or 2026-10-19')], {}),
    ([('Price', 'Adult {price} — official page, checked 2026-11-04')], {}),
    ([('Price', 'Adult {price}')], {'last_verified': '2026-11-04'}),
    ([('Price', 'Adult {price}')], {'last_verified': ''}),
])
def test_unqualified_headline_is_pending_after_load(load_price_case, terms, csv_fields):
    today = dt.date(2026, 11, 3)
    v = load_price_case('grand-josun-busan', terms, today, **csv_fields)
    card = B.venue_card(v, today)
    assert v['price'] is v['price_term'] is v['price_checked'] is None
    assert 'data-price="0"' in card and 'price-short' not in card
    assert numeric_price_part(v, today) == ''


def test_partial_box_stays_pending_with_an_official_sounding_term(load_price_case):
    today = dt.date(2026, 11, 3)
    v = load_price_case('crossfit-grove-gunja', [
        ('Drop-in, single', '{price} — official source: venue site, checked 2026-10-18'),
    ], today)
    assert v['price'] is None
    assert 'data-price="0"' in B.venue_card(v, today)
    assert numeric_price_part(v, today) == ''


def test_price_can_use_the_venue_check_date(venues):
    today = dt.date(2026, 11, 3)
    v = venues['grand-josun-busan']
    numeric = numeric_price_part(v, today)
    assert v['price_checked'] == v['last_verified']
    assert '₩55,000' in numeric and 'Official source' in numeric
    assert f'Checked {B.dot(v["last_verified"])}' in numeric
    assert 'data-price="1"' in B.venue_card(v, today)


SUNDAY_TODAY = dt.date(2026, 11, 19)
VENUE_URL = 'https://example.org/venue'
SUNDAY_URL = 'https://example.org/sunday'
SHIM_URL = 'https://example.org/closures'
WEEKDAY_HOURS = 'Mon–Fri 06:30–23:55 · Sat 13:00–17:30'
REPLY_HOURS = 'Classes at 09:00 · 10:00 · 11:00 · Closed the 4th Sunday of each month (venue reply 10/17)'


def sunday_evidence(status, age):
    return dict(status=status, source_kind='official_page', source_url=SUNDAY_URL,
                checked_on=(SUNDAY_TODAY - dt.timedelta(days=age)).isoformat())


def sunday_closure(**fields):
    return dict(closed='Sundays and public holidays', source='Official page',
                url=SHIM_URL, checked='2026-10-18') | fields


def sunday_fixture_cases():
    return {
        'fixture-sunday-open': dict(partial=True, evidence=sunday_evidence('yes', 91), legacy='no',
                                    facts=[('Hours', 'Weekdays 07:00–20:00')]),
        'fixture-sunday-closed': dict(sauna=True, venue_age=120, evidence=sunday_evidence('no', 90),
                                      legacy='yes', facts=[('Closed', 'Closed Sunday')]),
        'fixture-compound': dict(legacy='yes', facts=[('Hours (main box)', WEEKDAY_HOURS + ' · Closed Sunday')]),
        'fixture-branch': dict(csv_flag='no', facts=[("Citizens' Forest branch", 'Closed Sat and Sun')]),
        'fixture-reply': dict(csv_flag='yes', facts=[('Sunday', REPLY_HOURS)],
                              translations=[('일요일 운영', '일요일 운영')]),
        'fixture-shim': dict(partial=True, closure=sunday_closure(), facts=[('Closed', 'Sundays and public holidays')]),
        'fixture-temporary': dict(closure=sunday_closure(until='2026-11-20')),
        'fixture-reply-closure': dict(closure=sunday_closure(source='Venue reply')),
        'fixture-conflict': dict(evidence=sunday_evidence('no', 2), facts=[('Sunday', 'Open Sunday')]),
        'fixture-korean': dict(facts=[('Closed', 'Weekly closure')],
                               translations=[('휴무', '일요일 및 공휴일')]),
    }


def sunday_fixture_pages(tmp_path, monkeypatch, cases):
    """Load synthetic operations records through the same path as the release build."""
    content = tmp_path / 'content'
    content.mkdir()
    ko, en, closures = {}, {}, {}
    csv_rows = {'boxes.csv': [], 'saunas.csv': [], 'events.csv': []}
    for slug, case in cases.items():
        sauna = case.get('sauna', False)
        kind, csv_key = ('호텔 사우나', 'saunas') if sauna else ('크로스핏 박스', 'boxes')
        status = 'partial' if case.get('partial') else 'verified'
        facts = case.get('facts', [])
        ko[slug] = dict(slug=slug, csv=csv_key, kind=kind, title=slug, location='Seoul',
                        status=status, facts=case.get('translations', facts))
        en[slug] = dict(title=slug, location='Seoul', summary='', facts=facts)
        if 'evidence' in case:
            ko[slug]['sunday_evidence'] = case['evidence']
        if 'legacy' in case:
            ko[slug]['sunday'] = case['legacy']
        if 'closure' in case:
            closures[slug] = case['closure']
        checked = SUNDAY_TODAY - dt.timedelta(days=case.get('venue_age', 3))
        csv_rows[csv_key + '.csv'].append(dict(
            name=slug, city='Seoul', confidence='high (official page read)' if sauna else status,
            last_verified=checked.isoformat(), website=VENUE_URL, source_url=VENUE_URL, price_krw='',
            price_source_url=VENUE_URL, cf_listing_url='', address='', sunday_open=case.get('csv_flag', ''),
            drop_in_price_krw='', booking_url='',
        ))
    for filename, data in (('venues.json', ko), ('venues_en.json', en),
                           ('events.json', {}), ('events_en.json', {}), ('ads.json', {})):
        (content / filename).write_text(json.dumps(data), encoding='utf-8')
    with monkeypatch.context() as patch:
        patch.setattr(B, 'CLOSED_ALL', closures)
        patch.setattr(B, 'CLOSED_MONTH', {})
        patch.setattr(B, 'FOREIGNER', {})
        with patch.context() as load_patch:
            load_patch.setattr(B, 'ROOT', tmp_path)
            load_patch.setattr(B, 'read_csv', csv_rows.__getitem__)
            venues, events, *_ = B.load(SUNDAY_TODAY)
        site = B.Site(SUNDAY_TODAY, True)
        B.build(site, venues, events, SUNDAY_TODAY)
    return site.pages


def sunday_markup(pages, slug, label='Sunday'):
    card = next(card for card in pages['/finder/'].split('</article>') if f'data-slug="{slug}"' in card)
    badge = card.split('<div class="chips card-sunday">', 1)[1].split('</div>', 1)[0]
    page = pages[f'/finder/{slug}/']
    glance, notes = page.split('<h2>Source notes</h2>', 1)
    start = f'<strong>{label}:</strong> <span class="sunday-status">'
    assert glance.count(start) == 1
    assert notes.count(f'<th>{label}</th><td><span class="sunday-status">') == 1
    glance_row = glance.split(start, 1)[1].split('</div>', 1)[0]
    notes_row = notes.split(f'<th>{label}</th><td>', 1)[1].split('</td>', 1)[0]
    return card, badge, (glance_row, notes_row)


@pytest.mark.parametrize('status,age', [('yes', 90), ('yes', 91), ('no', 90), ('no', 91)])
def test_sunday_evidence_has_its_own_freshness(tmp_path, monkeypatch, status, age):
    slug = 'fixture-age'
    venue_age = 3 if age == 91 else 120
    cases = {slug: dict(partial=status == 'yes', sauna=status == 'no', venue_age=venue_age,
                        evidence=sunday_evidence(status, age))}
    pages = sunday_fixture_pages(tmp_path, monkeypatch, cases)
    checked = SUNDAY_TODAY - dt.timedelta(days=age)
    venue_checked = SUNDAY_TODAY - dt.timedelta(days=venue_age)
    card, badge, rows = sunday_markup(pages, slug)
    assert f'data-sunday="{status}"' in card
    assert B.SUN_TXT[status] in badge and (B.OUTDATED in badge) == (age == 91)
    assert f'Checked {B.dot(venue_checked)}' in card and B.dot(checked) not in card
    assert SUNDAY_URL not in card and '<dt>' not in card and '<table' not in card
    for row in rows:
        assert B.SUN_TXT[status] in row
        assert f'href="{SUNDAY_URL}"' in row and f'Checked {B.dot(checked)}' in row
        assert VENUE_URL not in row and B.dot(venue_checked) not in row
        assert (B.OUTDATED in row) == (age == 91)


@pytest.mark.parametrize('fields', [
    None, False, [], 'verified', {},
    {'status': 'unk'}, {'status': ['yes', 'no']},
    {'source_kind': 'third_party'}, {'source_kind': 'venue_reply'},
    {'source_url': ''}, {'source_url': '//example.org/sunday'}, {'source_url': 'ftp://example.org/sunday'},
    {'source_url': 'https:///sunday'}, {'source_url': 'https://example.org:invalid/sunday'},
    {'source_url': 'https://user:pass' + chr(64) + 'example.org/sunday'},
    {'checked_on': None}, {'checked_on': '11/18'}, {'checked_on': '2026.11.18'},
    {'checked_on': '2026-02-30'}, {'checked_on': '2026-11-18 or 2026-11-19'}, {'checked_on': '2026-11-20'},
])
def test_invalid_explicit_sunday_evidence_cannot_fall_back_to_shim(tmp_path, monkeypatch, fields):
    slug = 'fixture-invalid'
    if isinstance(fields, dict) and fields:
        fields = sunday_evidence('yes', 2) | fields
    pages = sunday_fixture_pages(tmp_path, monkeypatch, {
        slug: dict(evidence=fields, closure=sunday_closure(), legacy='yes', csv_flag='no'),
    })
    card, badge, rows = sunday_markup(pages, slug)
    assert 'data-sunday="unk"' in card
    assert B.SUN_TXT['unk'] in badge and B.OUTDATED not in badge
    for row in rows:
        assert B.PENDING in row and 'href=' not in row and 'Checked' not in row
    assert SHIM_URL not in pages[f'/finder/{slug}/']


def test_explicit_sunday_evidence_supersedes_valid_shim(tmp_path, monkeypatch):
    slug = 'fixture-supersedes'
    pages = sunday_fixture_pages(tmp_path, monkeypatch, {
        slug: dict(evidence=sunday_evidence('yes', 0), closure=sunday_closure()),
    })
    card, _, rows = sunday_markup(pages, slug)
    assert 'data-sunday="yes"' in card
    for row in rows:
        assert SUNDAY_URL in row and B.dot(SUNDAY_TODAY) in row and SHIM_URL not in row
    assert SHIM_URL not in pages[f'/finder/{slug}/']


@pytest.mark.parametrize('fields', [
    {'checked': ''}, {'checked': '10/18'}, {'checked': '2026-11-20'}, {'url': '/closures'},
    {'source': 'Official Instagram'}, {'closed': 'Closed Sunday this week'},
])
def test_closure_shim_requires_the_exact_dated_official_record(tmp_path, monkeypatch, fields):
    slug = 'fixture-shim-pending'
    pages = sunday_fixture_pages(tmp_path, monkeypatch, {slug: dict(closure=sunday_closure(**fields))})
    card, badge, rows = sunday_markup(pages, slug)
    assert 'data-sunday="unk"' in card and B.SUN_TXT['unk'] in badge
    for row in rows:
        assert B.PENDING in row and 'href=' not in row and 'Checked' not in row


def test_sunday_legacy_copy_and_closure_provenance(tmp_path, monkeypatch):
    pages = sunday_fixture_pages(tmp_path, monkeypatch, sunday_fixture_cases())
    for slug, label, claim in (
            ('fixture-compound', 'Sunday', 'Closed Sunday'),
            ('fixture-branch', 'Sunday', 'Closed Sat and Sun'),
            ('fixture-korean', 'Sunday', 'Weekly closure'),
            ('fixture-conflict', 'Sunday status', 'Sunday sources disagree'),
            ('fixture-temporary', 'Sunday', ''),
            ('fixture-reply-closure', 'Sunday', '')):
        card, badge, rows = sunday_markup(pages, slug, label)
        assert 'data-sunday="unk"' in card and B.SUN_TXT['unk'] in badge
        for row in rows:
            assert B.PENDING in row and claim in row
            assert 'href=' not in row and 'Checked' not in row
    compound = pages['/finder/fixture-compound/']
    assert compound.count(WEEKDAY_HOURS + '<small class="fact-source">Checked 2026.11.16') == 2
    assert WEEKDAY_HOURS + ' · Closed Sunday' not in compound
    _, _, branch_rows = sunday_markup(pages, 'fixture-branch')
    for row in branch_rows:
        assert f'<strong>{B.E("Citizens' Forest branch")}:</strong> Closed Sat and Sun' in row
    for slug, label in (('fixture-reply', 'Sunday status'), ('fixture-conflict', 'Sunday status')):
        page = pages[f'/finder/{slug}/']
        assert page.count('<strong>Sunday:</strong>') == page.count('<th>Sunday</th>') == 1
        _, _, rows = sunday_markup(pages, slug, label)
        assert all(B.PENDING in row for row in rows)
    reply = pages['/finder/fixture-reply/']
    reply_note = REPLY_HOURS + '<small class="fact-source">Venue reply · 10/17 (date not confirmed)'
    assert reply.count(reply_note) == 2 and 'Open Sunday (verified)' not in reply
    conflict = pages['/finder/fixture-conflict/']
    assert conflict.count('Open Sunday<small class="fact-source">Not confirmed') == 2
    assert conflict.count('Closed Sunday<small class="fact-source">Sunday sources disagree') == 2
    assert SUNDAY_URL not in conflict
    card, _, rows = sunday_markup(pages, 'fixture-shim')
    assert 'data-sunday="no"' in card and SHIM_URL not in card
    for row in rows:
        citation = f'Checked 2026.10.18 · <a href="{SHIM_URL}"'
        assert B.SUN_TXT['no'] in row and row.count(citation) == 2
        assert '2026.11.16' not in row and VENUE_URL not in row


def test_opposing_compound_sunday_claims_keep_status_pending(tmp_path, monkeypatch):
    slug = 'fixture-contradictory'
    claim = 'Open Sunday · Closed Sunday'
    pages = sunday_fixture_pages(tmp_path, monkeypatch, {
        slug: dict(evidence=sunday_evidence('no', 2), facts=[('Hours', claim)]),
    })
    card, _, rows = sunday_markup(pages, slug)
    assert 'data-sunday="unk"' in card
    assert all(claim in row and 'href=' not in row and 'Checked' not in row for row in rows)


@pytest.mark.parametrize('age', [90, 91, 0, -1])
def test_reply_price_terms_keep_source_and_age_boundary(venues, age):
    # The price fact's dated evidence lives on the terms; the outdated label tracks
    # the reply's own date at the exact 90/91-day boundary, not the venue's age.
    v = deepcopy(venues['sisu-house'])
    today = dt.date(2026, 11, 3)
    checked = today - dt.timedelta(days=age)
    v['last_verified'] = today - dt.timedelta(days=200)  # venue age must not leak in
    v['facts'] = [(key, (f'Spa Basic ₩79,000 — venue reply, {checked.isoformat()}' if key == 'Price' else val))
                  for key, val in v['facts']]
    html = B.price_html(v, today)
    assert 'Official source' not in html
    assert f'Venue reply · {checked.isoformat()}' in html
    assert (B.OUTDATED in html) == (age > 90)
