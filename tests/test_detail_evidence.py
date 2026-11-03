"""Evidence boundaries: no venue-level status upgrade, invented facilities or badges."""
from copy import deepcopy
import datetime as dt
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'site'))
import build_site as B


@pytest.fixture(scope='module')
def venues():
    rows, *_ = B.load(dt.date(2026, 11, 3), None)
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
    note = B.fact_provenance(v, value)
    assert {'fact': 'Official source', 'unverified': 'Unconfirmed', 'source_claim': 'Source claim'}[status] in note
    if status != 'fact':
        assert 'Official source' not in note
        assert 'href=' not in note


@pytest.mark.parametrize('value', ['', B.UNKNOWN, B.UNKNOWN + ' (temperature)', 'pending', 'not confirmed', 'not yet announced'])
def test_unknown_has_no_source_provenance(venues, value):
    assert B.fact_provenance(venues['orakai-cheonggyesan'], value) == ''


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
    note = B.fact_provenance(v, value)
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
    assert 'href=' not in B.fact_provenance(v, '급냉탕')


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
    assert B.UNKNOWN in B.fact_lines([('Sauna temperature', B.UNKNOWN)], v)


def test_rich_groups_include_original_facilities_and_programs(venues):
    for v in venues.values():
        groups = dict(B.fact_groups(v))
        assert 'Facilities & temperatures' in groups and 'Programs' in groups
        for label, value in v['facts']:
            if label in ('Facilities', 'Equipment (official)'):
                assert (label, value) in groups['Facilities & temperatures']
    holis = venues['holis-training-club']
    assert 'Group session' in B.price_html(holis)
    assert 'Day pass ₩30,000' in B.price_html(holis)
    assert '₩40,000' in B.price_short(holis)
    assert 'Day pass' not in B.price_short(holis)


def test_third_party_price_and_hour_clues_are_redacted_everywhere(venues):
    for v in venues.values():
        for key, value in v['facts']:
            if any(word in value.lower() for word in ('unverified clue', 'third-party', 'blog')) and (
                    key.lower().startswith(('price', 'hours')) or '₩' in value):
                shown = B.display_value(v, key, value)
                assert 'Unconfirmed' in shown and '₩' not in shown
                assert value not in B.fact_lines([(key, value)], v)
                assert 'Unconfirmed' in B.fact_provenance(v, shown)


def test_explicit_stale_marker_and_venue_staleness_survive(venues):
    v = deepcopy(venues['orakai-cheonggyesan'])
    v['stale'] = True
    assert 'Outdated' in B.fact_provenance(v, 'Dry sauna')
    v['stale'] = False
    assert 'Outdated' in B.fact_provenance(v, 'Dry sauna (older than 90 days)')
