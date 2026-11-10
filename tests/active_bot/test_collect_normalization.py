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
