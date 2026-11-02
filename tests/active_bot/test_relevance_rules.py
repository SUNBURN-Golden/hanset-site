import pytest
from hanset_active.extract import html, fields

@pytest.mark.parametrize('marker', ['tripadvisor-wrap', 'trip-lst', 'Review', 'COMMENT', 'reply', 'guestbook'])
def test_ugc_removed(marker):
    doc = html.extract(f'<main><p>hot baths</p><div id="{marker}"><table><tr><td>foreigners welcome</td></tr></table></div></main>')
    assert 'foreigners' not in doc['text']
    assert doc['structured']['tables'] == []

@pytest.mark.parametrize('text', ['steamed anglerfish', 'steamy', '.fa-steam:before', 'ion-steam', '_steam', 'cold shower', 'hot baths_identifier'])
def test_no_bath_fragments(text):
    assert not [r for r in fields.extract(text) if r['field'] == 'bath_composition']

@pytest.mark.parametrize('text,expected', [
    ('hot/cold baths', {'온탕', '냉탕'}), ('cold & hot baths', {'온탕', '냉탕'}),
    ('hot/cold/very-hot baths', {'온탕', '냉탕', '열탕'}), ('cold and very hot baths', {'냉탕', '열탕'}),
    ('sauna (dry+wet)', {'건식', '습식'}), ('saunas (dry and wet)', {'건식', '습식'}),
    ('sauna (wet/dry)', {'건식', '습식'}), ('steam', {'습식'}),
])
def test_explicit_baths(text, expected):
    assert set(next(r['value'] for r in fields.extract(text) if r['field'] == 'bath_composition')) == expected

@pytest.mark.parametrize('cue', ['maintenance', '점검', '정기 청소', 'cleaning', '청소 시간'])
def test_maintenance_not_hours(cue):
    rows = fields.extract(f'{cue} 09:00-10:00\nOperating hours 06:00-22:00')
    assert [r['value']['hours'] for r in rows if r['field'] == 'hours'] == ['06:00-22:00']
