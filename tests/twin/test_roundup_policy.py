"""Synthetic portal evidence and re-extraction regressions."""
import json
import pytest
from pathlib import Path

from hanset_active.extract import fields
from hanset_twin import stages
from tests.twin.test_twin import env  # noqa: F401


def test_retention_hours():
    for text in ('재가입 방지를 위해 24시간 동안 이메일 주소 보관 후 파기',
                 '이용 정보를 24시간 보관', '24시간', '운영 안내; 24시간 동안 보관'):
        assert not [r for r in fields.extract(text) if r['field'] == 'hours']
    assert [r['value']['hours'] for r in fields.extract('24시간 운영') if r['field'] == 'hours'] == ['24_hours']


@pytest.mark.parametrize('path,tag', [('editorspicks/baths', 'h2'), ('article/baths', 'h2'), ('best/baths', 'p'), ('article/baths', 'p')])
def test_roundup_policy_reextract(env, monkeypatch, path, tag):
    a, twin, run, csv = env
    monkeypatch.setattr(stages, '_portals', lambda: {'portal.example'})
    a.venue('alpha', 'Alpha Spa', 'https://example.com')
    quote_rows = [
        ('hours', {'hours': '06:30-22:00'}, 'fact', '영업시간: 06:30-22:00'),
        ('bath_composition', ['냉탕'], 'fact', '냉탕'),
        ('bath_composition', ['건식'], 'fact', '건식'),
        ('hours', {'hours': '24_hours'}, 'fact', '운영시간: 24시간 운영'),
        ('bath_composition', ['온탕'], 'fact', '온탕'),
        ('bath_composition', ['습식'], 'fact', '습식'),
    ]
    raw = a.doc('https://portal.example/' + path, '2026-11-02T09:00:00+09:00', quote_rows, 'alpha', 'official_directory')
    path = a.db.execute('SELECT blob_path FROM raw_documents WHERE raw_id=?', (raw,)).fetchone()[0]
    Path(path).write_text(f'<title>Seoul bathhouse</title><main><{tag}>Alpha Spa</{tag}><p>영업시간: 06:30-22:00</p><p>냉탕</p><p>건식</p><{tag}>Beta Spa</{tag}><p>운영시간: 24시간 운영</p><p>온탕</p><p>습식</p></main>')
    for url, field, value, quote in [
        ('https://example.com/privacy', 'hours', {'hours': '24_hours'}, '24시간 운영'),
        ('https://example.com/terms', 'website', 'https://example.com/privacy', 'Privacy'),
        ('https://example.com/links', 'website', 'https://portal.example', 'Portal'),
    ]:
        a.doc(url, '2026-11-02T09:00:00+09:00', [(field, value, 'fact', quote)], 'alpha')
    original = stages._active_obs_verdict
    monkeypatch.setattr(stages, '_active_obs_verdict', lambda *args: None)
    assert run()[1] == 'ok'
    monkeypatch.setattr(stages, '_active_obs_verdict', original)
    stages.reextract(stages.Ctx(twin, a.db, csv_path=csv, run_id='fixed'))
    rows = list(twin.execute('SELECT * FROM candidates'))
    for row in rows:
        if row['quote'] in ('운영시간: 24시간 운영', '온탕', '습식'):
            assert row['verdict_reason'] == 'roundup_other_section'
        elif row['field'] == 'website':
            assert row['verdict_reason'] == 'not_venue_website'
        elif row['source_url'].endswith('/privacy'):
            assert row['verdict_reason'] == 'policy_page'
        else:
            assert row['verdict'] == 'accepted'
            if row['field'] == 'bath_composition':
                assert json.loads(row['value_json']) == ['건식', '냉탕']
    assert not twin.execute("SELECT 1 FROM conflicts WHERE state='open'").fetchone()
