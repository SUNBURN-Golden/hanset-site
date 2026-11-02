"""Hardened fields.py rules (2026-10-29): unconfirmed text yields nothing, tattoo policies map
exactly as stated, cold-plunge mentions require an explicit bath, rapid-cold plunge is never
inferred. All text is synthetic."""
import pytest
from hanset_active.extract.fields import extract

def fields(text):
    return [(r['field'], r['value']) for r in extract(text)]

def bath_tokens(text):
    return next((r['value'] for r in extract(text) if r['field'] == 'bath_composition'), [])

@pytest.mark.parametrize('text', [
    'Prices/day pass/tattoo/foreigner rules Not confirmed.',
    'Prices/tattoo Not confirmed. 전화번호 게시하지 않음.',
    'Price not shown on official page',
    'Tattoo policy 미확인',
    'Hours 확인 안 됨',
    'unconfirmed from an official page',
    'Not applicable / not public',
    'Tattoos allowed but price not listed.',
])
def test_unconfirmed_boilerplate_yields_nothing(text):
    assert extract(text) == []

def test_soft_hedge_stays_ambiguous_not_silent():
    rows = [r for r in extract('Day pass 25000 KRW maybe') if r['field'] == 'price_day_pass']
    assert rows and all(r['ambiguous'] for r in rows)  # kept for review, never a value

@pytest.mark.parametrize('text,value', [
    ('Tattoos not admitted.', 'not_allowed'),
    ('문신·피부병 환자 출입금지', 'not_allowed'),
    ('tattoo >15cm needs patch', 'cover_required'),
    ('tattoos 15x15cm or larger permitted only when covered with a patch or personal rash guard', 'cover_required'),
    ('문신 커버/스티커 필요', 'cover_required'),
    ('visitors with tattoos may be restricted', 'conditional'),
    ('tattoos must be covered', 'cover_required'),
    ('문신 허용', 'allowed'),
    ('tattoos not prohibited', 'allowed'),
])
def test_tattoo_maps_exactly_as_stated(text, value):
    assert ('tattoo_policy', value) in fields(text)

@pytest.mark.parametrize('text', [
    'hot/cold baths',
    'cold/hot baths',
    'hot and cold baths',
    'hot/cold/very-hot baths',
    'Four hot baths, one cold',
    'two cold baths and a hot bath',
    'cold bath',
    '냉탕',
    'cold plunge',
])
def test_explicit_cold_mentions_give_cold_bath(text):
    assert '냉탕' in bath_tokens(text)

@pytest.mark.parametrize('text', [
    '14°C cold shower',          # a cold shower is not a cold plunge
    'cold showers available',
    'Six large baths',           # baths without a cold mention
    'no cold bath',
    'hot baths only',
])
def test_no_cold_plunge_without_an_explicit_cold_bath(text):
    assert '냉탕' not in bath_tokens(text)

def test_rapid_cold_plunge_never_inferred_from_cold_baths():
    for text in ('hot/cold baths available', 'Four hot baths, one cold', 'cold plunge 12°C'):
        assert not any(r['field'] == 'rapid_cold_plunge' for r in extract(text))

@pytest.mark.parametrize('text,value', [
    ('급냉탕 있음', True),
    ('급냉탕 없음', False),
    ('rapid cold plunge available', True),
])
def test_rapid_cold_plunge_only_when_explicitly_named(text, value):
    assert ('rapid_cold_plunge', value) in fields(text)

def test_english_bath_words_map_to_canonical_tokens():
    assert set(bath_tokens('Finnish dry and wet saunas, steam and ice bath')) >= {'건식', '습식', '아이스배스'}
    assert '건식' in bath_tokens('wet+dry sauna')

@pytest.mark.parametrize('text', ['hot baths and one cold shower', 'cold room beside hot baths', 'cold air and baths'])
def test_unrelated_cold_words_are_not_baths(text):
    assert '냉탕' not in bath_tokens(text)


def test_tattoo_patch_mention_alone_is_not_permission():
    assert not any(r['field'] == 'tattoo_policy' for r in extract('Tattoo patches for sale'))
    assert ('tattoo_policy', 'not_allowed') in fields('Tattoos not admitted even with a patch')
