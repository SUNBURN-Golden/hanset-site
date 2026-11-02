"""Local evidence regressions; all sources are synthetic."""
import pytest
from hanset_active.extract import fields
from hanset_twin import ontology
from tests.twin.test_twin import env  # noqa: F401


def values(text, field):
    return [r['value'] for r in fields.extract(text) if r['field'] == field]


@pytest.mark.parametrize('cue', ['Lunch Break', 'break time', '브레이크타임', '휴게시간', '점심시간', '청소시간', 'cleaning time'])
def test_local_break(cue):
    text = f'Weekdays 11:00 - 21:00 & Saturdays 10:00 - 19:00 ({cue}: 13:00~14:00)(KST)'
    assert [v['hours'] for v in values(text, 'hours')] == ['11:00-21:00', '10:00-19:00']


@pytest.mark.parametrize('token', ['steam sauna', 'steam room', 'steam', '스팀', '스팀사우나', '스팀 사우나'])
def test_steam(token):
    assert values('Dry sauna, ' + token, 'bath_composition') == [['건식', '습식']]
    assert values('dry + wet sauna', 'bath_composition') == [['건식', '습식']]
    assert ontology.map_field('bath_composition', ['건식', '스팀']) == ontology.map_field('bath_composition', ['건식', '습식'])


@pytest.mark.parametrize('name', ['급냉탕', '급랭탕', '초냉탕', 'rapid cold plunge', 'extra-cold plunge', 'ice-cold plunge'])
def test_rapid(name):
    assert values(name + ' available', 'rapid_cold_plunge') == [True]
    assert values(name + ' not available', 'rapid_cold_plunge') == [False]
    assert values(name + ' 5°C', 'rapid_cold_plunge') == []
    assert values('Spa features ' + name, 'rapid_cold_plunge') == [True]
    assert values('Spa equipped with ' + name, 'rapid_cold_plunge') == [True]
    assert values('Spa not equipped with ' + name, 'rapid_cold_plunge') == [False]


@pytest.mark.parametrize('text', ['외국인 이용 가능', '사우나 외국인 고객 환영', 'Sauna foreign guests welcome', 'Spa international visitors allowed', 'Sauna English-speaking staff', 'Spa English available'])
def test_foreigner(text):
    assert values(text, 'foreigner_policy')
    assert values('Sauna foreigners not allowed', 'foreigner_policy') == ['not_allowed']


@pytest.mark.parametrize('text', ['Tourist Pass for Foreigners available', 'Mobile Passport required', 'Privacy policy passport copy required', 'I think sauna foreigners very welcome', '저는 사우나 외국인 이용 가능 후기'])
def test_not_policy(text):
    assert values(text, 'foreigner_policy') == []


def test_twin_rejections(env):
    a, twin, run, _ = env
    a.venue('alpha::', 'Sauna Alpha', 'https://spa.example.com')
    quote = 'Weekdays 11:00 - 21:00 & Saturdays 10:00 - 19:00 (Lunch Break: 13:00~14:00)(KST)'
    a.doc('https://spa.example.com/hours', '2026-10-01T09:00:00+09:00', [('hours', {'hours': '13:00-14:00'}, 'fact', quote)], 'alpha::')
    for field in ('foreigner_policy', 'tattoo_policy'):
        a.doc('https://spa.example.com/' + field, '2026-10-01T09:00:00+09:00', [(field, 'accepted', 'fact', 'I think sauna foreigners and tattoos welcome')], 'alpha::')
    assert run()[1] == 'ok'
    reasons = {r['field']: r['verdict_reason'] for r in twin.execute('SELECT * FROM candidates')}
    assert reasons['hours'] == 'break_hours'
    assert reasons['foreigner_policy'] == reasons['tattoo_policy'] == 'user_review_text'


def test_steam_sources_and_legacy_facts_do_not_conflict(env):
    a, twin, run, _ = env
    a.venue('alpha::', 'Sauna Alpha', 'https://spa.example.com')
    for path, quote in [('en', 'Dry sauna, Steam sauna'), ('verified', 'dry + wet sauna')]:
        value = values(quote, 'bath_composition')[0]
        a.doc('https://spa.example.com/' + path, '2026-10-01T09:00:00+09:00', [('bath_composition', value, 'fact', quote)], 'alpha::')
    assert run()[1] == 'ok'
    assert not twin.execute("SELECT 1 FROM conflicts WHERE state='open'").fetchone()
    row = dict(twin.execute("SELECT * FROM facts WHERE attribute='bath_list' AND effective_to IS NULL LIMIT 1").fetchone())
    row.update(fact_id='legacy-steam', value_json='["건식","스팀"]', value_hash='legacy-steam', slot_key='legacy-slot')
    twin.execute('INSERT INTO facts (' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))
    assert ontology.refresh_conflicts(twin) == 0
    assert not twin.execute("SELECT 1 FROM conflicts WHERE state='open'").fetchone()
