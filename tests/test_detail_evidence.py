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
