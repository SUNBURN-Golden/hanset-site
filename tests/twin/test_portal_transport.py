"""Twin: portal domains never count as a venue's own site; transport timetables are not hours."""
import json
from pathlib import Path
from hanset_active.extract import fields
from hanset_twin import stages
from tests.twin.test_twin import env  # noqa: F401  (fixture)


def test_fields_drop_transport_hours():
    hours = lambda t: [r['value']['hours'] for r in fields.extract(t) if r['field'] == 'hours']
    assert hours('운행시간: 06:00~21:00') == []
    assert hours('Shuttle bus 07:30-22:30') == []
    assert hours('운영시간 06:00~21:00') == ['06:00-21:00']
    assert hours('셔틀버스 운행 06:00~22:00, 사우나 07:00~21:00') == ['07:00-21:00']


def test_portal_pages_need_venue_title_and_no_portal_brand(env, monkeypatch):
    a, twin, run, _ = env
    monkeypatch.setattr(stages, '_portals', lambda: frozenset({'portal.example'}))
    a.venue('alpha::', 'Sauna Alpha', 'https://korean.portal.example')
    a.db.execute('UPDATE venues SET domains=? WHERE venue_key=?', (json.dumps(['portal.example']), 'alpha::')); a.db.commit()
    other = a.doc('https://korean.portal.example/other/P2', '2026-10-01T09:00:00+09:00', [('hours', {'hours': '05:00-23:00'}, 'fact', '05:00-23:00')], 'alpha::', 'official_directory')
    Path(a.db.execute('SELECT blob_path FROM raw_documents WHERE raw_id=?', (other,)).fetchone()[0]).write_text('<title>Sample Bathhouse | Portal</title><main><p>05:00-23:00</p></main>')
    a.doc('https://korean.portal.example/alpha/P1', '2026-10-01T09:00:00+09:00', [('hours', {'hours': '06:00-22:00'}, 'fact', '06:00-22:00')], 'alpha::', 'official_directory')
    shuttle = a.doc('https://hotel.example/location', '2026-10-01T09:00:00+09:00', [('hours', {'hours': '06:00-21:00'}, 'fact', '운행시간: 06:00~21:00')], 'alpha::')
    Path(a.db.execute('SELECT blob_path FROM raw_documents WHERE raw_id=?', (shuttle,)).fetchone()[0]).write_text('<title>Sauna Alpha</title><main><p>운행시간: 06:00~21:00</p></main>')
    assert run()[1] == 'ok'
    verdict = lambda url: tuple(twin.execute('SELECT verdict,verdict_reason FROM candidates WHERE source_url=?', (url,)).fetchone())
    assert verdict('https://korean.portal.example/other/P2') == ('rejected', 'off_topic_page')
    assert verdict('https://korean.portal.example/alpha/P1')[1] is None
    assert verdict('https://hotel.example/location') == ('rejected', 'transport_hours')
    names = {r[0] for r in twin.execute("SELECT name FROM entities WHERE entity_type='venue'")}
    assert 'portal.example' not in names and not any('portal' in (n or '') for n in names)
    assert not twin.execute("SELECT 1 FROM external_ids WHERE external_id LIKE '%portal.example%'").fetchone()


def test_stale_policy_value_from_old_rule_is_rejected(env):
    a, twin, run, _ = env
    a.venue('alpha::', 'Sauna Alpha', 'https://spa.example')
    quote = '문신이 있는 경우 이용이 제한될 수 있습니다.'
    a.doc('https://spa.example/rules', '2026-10-01T09:00:00+09:00', [('tattoo_policy', 'not_allowed', 'fact', quote)], 'alpha::')
    a.doc('https://spa.example/rules', '2026-10-02T09:00:00+09:00', [('tattoo_policy', 'conditional', 'fact', quote)], 'alpha::')
    assert run()[1] == 'ok'
    rows = {json.loads(r['value_json']): r['verdict_reason'] for r in twin.execute("SELECT value_json,verdict_reason FROM candidates WHERE field='tattoo_policy'")}
    assert rows == {'not_allowed': 'value_no_longer_matches', 'conditional': None}
    assert [json.loads(r[0]) for r in twin.execute("SELECT value_json FROM facts WHERE attribute='policy' AND effective_to IS NULL AND status!='unverified'")] == ['conditional']
