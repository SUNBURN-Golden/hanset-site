"""Conservative comparison does not erase the meaning of qualifiers."""
import copy

import pytest

from hanset_active.collect.normalize import normalize_value


@pytest.mark.parametrize('left,right', [
    ({'hours': '9:00 – 17:00', 'scope': 'weekday'}, {'hours': '09:00-17:00', 'scope': 'weekday'}),
    ({'amount': '28,000원', 'ticket': 'day_pass'}, {'amount': 28000, 'ticket': 'day_pass'}),
    ({'amount': 'KRW 28,000', 'ticket': 'day_pass'}, {'amount': 28000, 'ticket': 'day_pass'}),
    ({'amount': '28000 KRW', 'ticket': 'drop_in'}, {'amount': 28000, 'ticket': 'drop_in', 'currency': 'KRW'}),
])
def test_equivalent_shapes(left, right):
    field = 'hours' if 'hours' in left else 'price_day_pass'
    before = copy.deepcopy(left)
    a, b = normalize_value(field, left), normalize_value(field, right)
    assert a == b and a['reason'] is None
    assert left == before


@pytest.mark.parametrize('field,value', [
    ('hours', {'hours': '25:00-17:00'}),
    ('hours', {'hours': '09:70-17:00'}),
    ('hours', {'hours': '00:00-00:00'}),
    ('hours', {'hours': '00:00-24:00'}),
    ('hours', {'hours': '09:00-17:00', 'facility': None}),
    ('hours', {'hours': '09:00-17:00', 'unknown_qualifier': 'x'}),
    ('price_day_pass', {'amount': '28000', 'ticket': 'day_pass'}),
    ('price_day_pass', {'amount': True, 'ticket': 'day_pass'}),
    ('price_day_pass', {'amount': 28000, 'ticket': 'day_pass', 'currency': 'USD'}),
    ('price_day_pass', {'amount': 28000, 'ticket': 'day_pass', 'duration_hours': False}),
    ('price_day_pass', {'amount': 28000}),
    ('rapid_cold_plunge', 1),
    ('sauna_temp_c', float('nan')),
    ('cold_bath_temp_c', [20, 10]),
    ('foreigner_policy', 'unknown'),
    ('bath_composition', ['온탕', False]),
    ('unsupported', 'text'),
])
def test_invalid_and_ambiguous(field, value):
    assert normalize_value(field, value)['reason']


@pytest.mark.parametrize('qualifier,a,b', [
    ('scope', 'weekday', 'weekend'), ('facility', 'sauna', 'pool'),
    ('scope', None, 'unspecified'),
])
def test_hours_qualifiers_are_not_wildcards(qualifier, a, b):
    left = {'hours': '09:00-17:00', **({qualifier: a} if a else {})}
    right = {'hours': '09:00-17:00', qualifier: b}
    assert normalize_value('hours', left)['qualifiers'] != normalize_value('hours', right)['qualifiers']


@pytest.mark.parametrize('qualifier,value', [('audience', 'child'), ('duration_hours', 2), ('ticket', 'drop_in'), ('scope', 'weekend')])
def test_price_qualifiers_are_preserved(qualifier, value):
    base = {'amount': 28000, 'ticket': 'day_pass', 'scope': 'weekday', 'audience': 'adult', 'duration_hours': 3}
    assert normalize_value('price_day_pass', base)['qualifiers'] != normalize_value('price_day_pass', {**base, qualifier: value})['qualifiers']


def test_other_fields_use_typed_equality_and_only_baths_are_sets():
    assert normalize_value('bath_composition', ['냉탕', '온탕', '냉탕']) == normalize_value('bath_composition', ['온탕', '냉탕'])
    assert normalize_value('closures', 'Closed Monday')['comparison_key'] != normalize_value('closures', 'Closed on Monday')['comparison_key']
    assert normalize_value('sauna_temp_c', 80)['comparison_key'] != normalize_value('sauna_temp_c', [80, 80])['comparison_key']
    assert normalize_value('hours', {'hours': '24_hours'})['reason'] is None


@pytest.mark.parametrize('field,quote,value', [
    ('hours', '사우나 평일 09:00-17:00', {'hours': '09:00-17:00', 'scope': 'weekday', 'facility': 'sauna'}),
    ('price_day_pass', 'Adult sauna day pass: 20,000 KRW', {'amount': 20000, 'scope': 'unspecified',
                                                          'facility': 'sauna', 'ticket': 'day_pass', 'audience': 'adult'}),
    ('price_day_pass', 'Weekend drop-in price: 18000 KRW for 2 hours',
     {'amount': 18000, 'ticket': 'drop_in', 'scope': 'weekend', 'duration_hours': 2}),
    ('price_day_pass', 'Walk-in admission 18000 KRW',
     {'amount': 18000, 'ticket': 'drop_in', 'scope': 'unspecified'}),
    ('price_day_pass', '현장 입장 18000원',
     {'amount': 18000, 'ticket': 'drop_in', 'scope': 'unspecified'}),
    ('sauna_temp_c', '건식 사우나 82~90°C', [82, 90]),
    ('cold_bath_temp_c', '냉탕 12°C', 12),
    ('tattoo_policy', 'Tattoo cover patches required', 'cover_required'),
    ('foreigner_policy', 'Foreign visitors are welcome at the sauna', 'accepted'),
    ('drop_in_available', 'Walk-ins are welcome', True),
    ('drop_in_available', '드롭인 불가', False),
    ('parking', '주차 가능합니다', True),
    ('parking', 'No parking', False),
    ('showers', 'Showers provided', True),
    ('showers', '샤워실 미운영', False),
    ('showers', '샤워실 운영하지 않습니다', False),
])
def test_shipping_fields_ground_and_compare(field, quote, value):
    from hanset_active.collect.ground import parse_items

    result, = parse_items({'items': [{'index': 0, 'claims': [{'field': field, 'value': value, 'quote': quote}]}]},
                          [{'text': quote}], web=True)
    claims, dropped = result
    assert not dropped and claims[0]['value'] == value
    assert normalize_value(field, value)['reason'] is None


def test_price_alone_silence_questions_and_event_dates_do_not_create_availability():
    from hanset_active.collect.ground import FIELDS
    from hanset_active.extract.fields import extract

    text = ('드롭인 요금 18000원. Parking available? Parking is available to members only. '
            'Showers. Event dates 2026-11-24 to 2026-11-25.')
    assert {row['field'] for row in extract(text)} == {'price_day_pass'}
    assert 'event_dates' not in FIELDS
    assert normalize_value('event_dates', '2026-11-24')['reason']
    assert normalize_value('parking', 'available')['reason']
