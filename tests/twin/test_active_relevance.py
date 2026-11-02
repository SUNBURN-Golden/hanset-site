from tests.twin.test_twin import Active, CSV_HEAD
from hanset_twin import stages
from hanset_twin.store import connect, open_active_readonly
from hanset_twin.daemon import run_once


def test_gate_and_reextract_history(tmp_path, monkeypatch):
    a = Active(tmp_path / 'active.sqlite3')
    a.venue('example', 'Example Spa', 'https://spa.example')
    cases = [
        ('https://portal.example/spa', 'Example Spa | Portal', '<p>cold baths</p><div class="tripadvisor-wrap"><div class="trip-lst-wrap"><ul class="trip-lst"><li><dd>foreigners welcome</dd></li></ul></div></div>', 'foreigner_policy', 'accepted', 'foreigners welcome', 'quote_not_in_main_text'),
        ('https://portal.example/diner', 'Sample Diner | Portal', '<p>09:00-18:00</p>', 'hours', {'hours': '09:00-18:00'}, '09:00-18:00', 'off_topic_page'),
        ('https://portal.example/style.css', '', '.fa-steam:before', 'bath_composition', ['스팀'], '.fa-steam:before', 'asset_document'),
        ('https://spa.example/maintenance', 'Schedule', '<p>maintenance 09:00-10:00</p>', 'hours', {'hours': '09:00-10:00'}, 'maintenance 09:00-10:00', 'maintenance_window'),
        ('https://spa.example/info', 'Facilities', '<p>cold baths</p>', 'bath_composition', ['냉탕'], 'cold baths', None),
        ('https://portal.example/factual', 'Example Spa | Portal', '<p>hot baths</p>', 'bath_composition', ['온탕'], 'hot baths', None),
        ('https://spa.example/blog-feed.xml', 'Events', '<p>07:30-17:00</p>', 'hours', {'hours': '07:30-17:00'}, '07:30-17:00', 'feed_post_not_current'),
        ('https://spa.example/missing', '', '', 'hours', {'hours': '08:00-20:00'}, '08:00-20:00', 'raw_unavailable'),
        ('https://portal.example/food', 'Example Spa | Portal', '<p>Example Spa serves agwi jjim (steamed anglerfish with bean sprouts).</p>', 'bath_composition', ['스팀'], 'Example Spa serves agwi jjim (steamed anglerfish with bean sprouts).', 'rule_no_longer_matches'),
        ('https://portal.example/saunas', 'Example Spa | Portal', '<p>Example Spa has a dry sauna and steam room.</p>', 'bath_composition', ['스팀'], 'Example Spa has a dry sauna and steam room.', None),
        ('https://spa.example/cleaning', 'Schedule', '<p>청소 시간 09:00-10:00</p>', 'hours', {'hours': '09:00-10:00'}, '청소 시간 09:00-10:00', 'break_hours'),
    ]
    for i, (url, title, body, field, value, quote, reason) in enumerate(cases):
        raw = a.doc(url, f'2026-10-{i+1:02d}T09:00:00+09:00', [(field, value, 'fact', quote)], 'example', 'official_directory' if 'portal' in url else 'own_site')
        path = a.db.execute('SELECT blob_path FROM raw_documents WHERE raw_id=?', (raw,)).fetchone()[0]
        from pathlib import Path
        Path(path).write_text(f'<title>{title}</title><main>{body}</main>')
        if reason == 'raw_unavailable':
            Path(path).unlink()
    csv = tmp_path / 'input.csv'
    csv.write_text(CSV_HEAD)
    db = connect(tmp_path / 'twin.sqlite3')
    active = open_active_readonly(a.path)
    gated = connect(tmp_path / 'gated.sqlite3')
    assert run_once(gated, active, csv_path=csv, lock_path=tmp_path / 'gated-lock')[1] == 'ok'
    for url, title, body, field, value, quote, reason in cases:
        candidate = gated.execute('SELECT * FROM candidates WHERE source_url=?', (url,)).fetchone()
        assert candidate['verdict_reason'] == reason
        assert candidate['verdict'] == ('rejected' if reason else 'accepted')
    assert gated.execute("SELECT count(*) FROM facts WHERE status='source_claim' AND value_json IS NOT NULL").fetchone()[0] >= 1
    original = stages._active_obs_verdict
    monkeypatch.setattr(stages, '_active_obs_verdict', lambda *args: None)
    assert run_once(db, active, csv_path=csv, lock_path=tmp_path / 'lock')[1] == 'ok'
    old_ids = {r[0] for r in db.execute('SELECT fact_id FROM facts')}
    monkeypatch.setattr(stages, '_active_obs_verdict', original)
    result = stages.reextract(stages.Ctx(db, active, csv_path=csv, run_id='reextract'))
    assert result['origins']['active_store']['items'] == len(cases)
    for url, title, body, field, value, quote, reason in cases:
        candidate = db.execute('SELECT * FROM candidates WHERE source_url=?', (url,)).fetchone()
        assert candidate['verdict_reason'] == reason
        assert candidate['verdict'] == ('rejected' if reason else 'accepted')
    assert old_ids <= {r[0] for r in db.execute('SELECT fact_id FROM facts')}
    assert db.execute("SELECT count(*) FROM facts WHERE closed_reason='evidence_removed'").fetchone()[0] >= 5
    assert db.execute("SELECT count(*) FROM corrections WHERE kind='evidence_removed'").fetchone()[0] >= 5
    before = [tuple(r) for r in db.execute('SELECT fact_id,effective_to,closed_reason FROM facts ORDER BY fact_id')]
    corrections = db.execute('SELECT count(*) FROM corrections').fetchone()[0]
    stages.reextract(stages.Ctx(db, active, csv_path=csv, run_id='again'))
    assert before == [tuple(r) for r in db.execute('SELECT fact_id,effective_to,closed_reason FROM facts ORDER BY fact_id')]
    assert corrections == db.execute('SELECT count(*) FROM corrections').fetchone()[0]


def test_reextract_does_not_replay_old_active_snapshots(tmp_path):
    a = Active(tmp_path / 'active.sqlite3')
    a.venue('example', 'Example Spa', 'https://spa.example')
    url = 'https://spa.example/info'
    a.doc(url, '2026-10-01T09:00:00+09:00', [('hours', {'hours': '06:00-22:00'}, 'fact', '06:00-22:00')], 'example')
    a.doc(url, '2026-10-02T09:00:00+09:00', [('hours', {'hours': '07:00-21:00'}, 'fact', '07:00-21:00')], 'example')
    csv = tmp_path / 'input.csv'
    csv.write_text(CSV_HEAD)
    db = connect(tmp_path / 'twin.sqlite3')
    active = open_active_readonly(a.path)
    assert run_once(db, active, csv_path=csv, lock_path=tmp_path / 'lock')[1] == 'ok'
    before = [tuple(r) for r in db.execute('SELECT fact_id,effective_to FROM facts ORDER BY fact_id')]
    for _ in range(2):
        stages.reextract(stages.Ctx(db, active, csv_path=csv, run_id='again'))
        assert before == [tuple(r) for r in db.execute('SELECT fact_id,effective_to FROM facts ORDER BY fact_id')]
    # Latest evidence becomes invalid: older snapshots must never revive on later runs.
    raw = a.db.execute('SELECT blob_path FROM raw_documents ORDER BY fetched_at DESC LIMIT 1').fetchone()[0]
    from pathlib import Path
    Path(raw).write_text('<main>No current hours stated</main>')
    stages.reextract(stages.Ctx(db, active, csv_path=csv, run_id='removed'))
    before = [tuple(r) for r in db.execute('SELECT fact_id,effective_to FROM facts ORDER BY fact_id')]
    stages.reextract(stages.Ctx(db, active, csv_path=csv, run_id='again'))
    assert before == [tuple(r) for r in db.execute('SELECT fact_id,effective_to FROM facts ORDER BY fact_id')]
    assert not db.execute("SELECT 1 FROM facts WHERE attribute='open_hours' AND value_json IS NOT NULL AND effective_to IS NULL").fetchone()
