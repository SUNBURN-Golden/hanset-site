"""Anonymized twin tests: all venues, URLs and values are fictional."""
import json
import sqlite3
from datetime import date
from pathlib import Path
import pytest
from hanset_active.db import connect as active_connect, deterministic_id
from hanset_twin.store import connect, open_active_readonly
from hanset_twin.daemon import run_once, charge, CostCapExceeded, ProcessLock, AlreadyRunning
from hanset_twin.query import q_rapid_cold_foreigner_day_pass, slot, closed_on

URL = 'https://sauna-alpha.example/info'
CSV_HEAD = 'name,name_ko,type,city,district,website,price_krw,price_note,notable,source_url,confidence,last_verified,notes,verification_status,verification_source_url,verification_date,verification_detail,official_contact\n'

class Active:
    def __init__(self, path):
        self.path = path
        self.db = active_connect(path)
    def venue(self, key, name, website):
        self.db.execute('INSERT OR IGNORE INTO venues(venue_id,venue_key,name,website,type,state) VALUES (?,?,?,?,?,?)',
                        (deterministic_id(key), key, name, website, 'sauna', 'active')); self.db.commit()
    def doc(self, url, fetched_at, obs, venue_key, source_type='own_site'):
        raw = deterministic_id(url, fetched_at)
        self.db.execute("INSERT INTO raw_documents(raw_id,url,canonical_url,domain,fetched_at,http_status,content_hash,outcome) VALUES (?,?,?,?,?,200,?, 'ok')",
                        (raw, url, url, 'example', fetched_at, deterministic_id('h', fetched_at, obs)))
        for i, (field, value, label, quote) in enumerate(obs):
            self.db.execute('''INSERT INTO observations(obs_id,venue_key,field,value_json,value_text,value_hash,label_status,source_url,source_type,quote,locator,raw_id,observed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''', (deterministic_id(raw, i), venue_key, field, json.dumps(value), str(value), deterministic_id(value),
                label, url, source_type, quote, json.dumps({'char_offsets': [i, i + 5]}), raw, fetched_at))
        self.db.commit()
        return raw

@pytest.fixture
def env(tmp_path):
    a = Active(tmp_path / 'active.sqlite3')
    csv = tmp_path / 'saunas.csv'; csv.write_text(CSV_HEAD)
    twin = connect(tmp_path / 'twin' / 'twin.sqlite3')
    def run(**kw):
        return run_once(twin, open_active_readonly(a.path), csv_path=csv, lock_path=tmp_path / 'twin' / 'lock', **kw)
    return a, twin, run, csv

def hours_facts(twin):
    return twin.execute("SELECT * FROM facts WHERE attribute='open_hours' AND status!='unverified' ORDER BY effective_from").fetchall()

def test_changed_value_closes_old_period_and_keeps_history(env):
    a, twin, run, _ = env
    a.venue('alpha::', 'Sauna Alpha', URL)
    a.doc(URL, '2026-10-01T09:00:00+09:00', [('hours', {'hours': '06:00-22:00', 'scope': 'unspecified'}, 'fact', '운영시간 06:00-22:00')], 'alpha::')
    assert run()[1] == 'ok'
    a.doc(URL, '2026-10-20T09:00:00+09:00', [('hours', {'hours': '07:00-21:00', 'scope': 'unspecified'}, 'fact', '운영시간 07:00-21:00')], 'alpha::')
    assert run()[1] == 'ok'
    old, new = hours_facts(twin)
    assert json.loads(old['value_json']) == '06:00-22:00' and old['effective_to'] == new['effective_from'] == '2026-10-20T09:00:00+09:00'
    assert old['superseded_by'] == new['fact_id'] and old['closed_reason'] == 'value_change' and new['effective_to'] is None
    assert old['status'] == new['status'] == 'fact' and new['quote'] and new['source_url'] == URL
    assert twin.execute("SELECT count(*) FROM corrections WHERE kind='value_change'").fetchone()[0] == 1
    bid = twin.execute("SELECT entity_id FROM entities WHERE entity_type='branch'").fetchone()[0]
    past = slot(twin, bid, 'hours', 'regular', 'open_hours', at='2026-10-05T00:00:00+09:00')
    assert [e['value'] for e in past['evidence']] == ['06:00-22:00']
    # Re-fetch with identical value only confirms; nothing new is written.
    a.doc(URL, '2026-10-25T09:00:00+09:00', [('hours', {'hours': '07:00-21:00', 'scope': 'unspecified'}, 'fact', '운영시간 07:00-21:00')], 'alpha::')
    run(); assert len(hours_facts(twin)) == 2

def test_facts_are_append_only(env):
    a, twin, run, _ = env
    a.venue('alpha::', 'Sauna Alpha', URL)
    a.doc(URL, '2026-10-01T09:00:00+09:00', [('hours', {'hours': '06:00-22:00'}, 'fact', '06:00-22:00')], 'alpha::')
    run()
    fid = hours_facts(twin)[0]['fact_id']
    with pytest.raises(sqlite3.IntegrityError): twin.execute("UPDATE facts SET value_json='\"x\"' WHERE fact_id=?", (fid,))
    with pytest.raises(sqlite3.IntegrityError): twin.execute('DELETE FROM facts WHERE fact_id=?', (fid,))

def test_conflicts_are_kept_not_overwritten(env):
    a, twin, run, _ = env
    a.venue('alpha::', 'Sauna Alpha', URL)
    a.doc(URL, '2026-10-01T09:00:00+09:00', [('hours', {'hours': '24_hours'}, 'fact', 'open 24 hours'), ('hours', {'hours': '09:00-10:00'}, 'fact', 'maintenance 9-10')], 'alpha::')
    a.doc('https://directory.example/alpha', '2026-10-02T09:00:00+09:00', [('cold_bath_temp_c', 15, 'fact', '냉탕 15도')], 'alpha::', 'official_directory')
    a.doc(URL, '2026-10-03T09:00:00+09:00', [('cold_bath_temp_c', 12, 'fact', '냉탕 12도')], 'alpha::')
    run()
    reasons = {r[0] for r in twin.execute("SELECT reason FROM conflicts WHERE state='open'")}
    assert reasons == {'same_source_multiple_values', 'sources_disagree'}
    assert twin.execute("SELECT count(*) FROM facts WHERE effective_to IS NOT NULL AND status!='unverified'").fetchone()[0] == 0

def test_different_branches_never_merge(env, tmp_path):
    a, twin, run, csv = env
    csv.write_text(CSV_HEAD + 'Chain Hotel North,,hotel sauna,Seoul,,https://chain.example/north,,,,https://chain.example/north,,2026-10-01,,,,,,\n'
                              'Chain Hotel South,,hotel sauna,Busan,,https://chain.example/south,,,,https://chain.example/south,,2026-10-01,,,,,,\n')
    a.venue('chainhoteleast::', 'Chain Hotel East', 'https://chain.example/east')
    a.venue('chainhotelnorth::', 'Chain Hotel North', 'https://chain.example/north')
    run()
    names = sorted(r[0] for r in twin.execute("SELECT name FROM entities WHERE entity_type='branch'"))
    assert names == ['Chain Hotel East', 'Chain Hotel North', 'Chain Hotel South']
    assert twin.execute("SELECT count(DISTINCT entity_id) FROM entities WHERE entity_type='venue'").fetchone()[0] == 1
    assert twin.execute("SELECT count(*) FROM review_queue WHERE kind='possible_same_branch'").fetchone()[0] == 1

def test_unverified_and_ai_never_fill_values(env):
    a, twin, run, _ = env
    a.venue('alpha::', 'Sauna Alpha', URL)
    a.doc(URL, '2026-10-01T09:00:00+09:00', [('price_day_pass', {'amount': 9000, 'ticket': 'day_pass'}, 'unverified', '요금 9,000원/11,000원?')], 'alpha::')
    a.doc('https://blog.naver.com/x', '2026-10-01T10:00:00+09:00', [('tattoo_policy', 'allowed', 'fact', '문신 가능')], 'alpha::', 'third_party')
    a.doc('https://listicle.example/top10', '2026-10-01T11:00:00+09:00', [('hours', {'hours': '05:00-23:00'}, 'fact', '05:00-23:00')], 'top10sauna::', 'third_party')
    run()
    assert twin.execute("SELECT count(*) FROM facts WHERE value_json IS NOT NULL").fetchone()[0] == 0
    assert twin.execute("SELECT count(*) FROM facts WHERE status='ai_inference'").fetchone()[0] == 0
    assert twin.execute("SELECT count(*) FROM facts WHERE status='unverified' AND effective_to IS NULL").fetchone()[0] >= 15
    kinds = {r[0] for r in twin.execute('SELECT kind FROM review_queue')}
    assert {'ambiguous_extraction', 'unresolved_subject'} <= kinds
    assert twin.execute("SELECT count(*) FROM work_items WHERE canonical_url LIKE '%naver%'").fetchone()[0] == 0

def test_representative_question_answers_with_evidence(env):
    a, twin, run, _ = env
    a.venue('alpha::', 'Sauna Alpha', URL)
    a.venue('beta::', 'Sauna Beta', 'https://sauna-beta.example')
    a.doc(URL, '2026-10-01T09:00:00+09:00', [
        ('rapid_cold_plunge', True, 'fact', '급냉탕 운영 (5도)'), ('foreigner_policy', 'accepted', 'fact', 'Foreign visitors welcome'),
        ('price_day_pass', {'amount': 25000, 'ticket': 'day_pass', 'scope': 'unspecified'}, 'fact', '1회 이용권 25,000원'),
        ('closures', '매월 첫째 수요일 휴무', 'fact', '매월 첫째 수요일 휴무')], 'alpha::')
    a.doc('https://sauna-beta.example', '2026-10-01T09:00:00+09:00', [('rapid_cold_plunge', False, 'fact', '급냉탕 없음')], 'beta::')
    run()
    ans = {x['name']: x for x in q_rapid_cold_foreigner_day_pass(twin, date(2026, 10, 28))}
    alpha = ans['Sauna Alpha']
    assert alpha['criteria'] == {'rapid_cold_plunge': 'yes', 'foreigner_allowed': 'yes', 'non_guest_access': 'unverified', 'day_pass': 'yes', 'open_today': 'open'}
    assert alpha['verdict'] == 'unknown' and alpha['answered_criteria'] == 4  # access never assumed
    ev = alpha['evidence']['rapid_cold_plunge'][0]
    assert ev['source_url'] == URL and ev['quote'] and ev['evidence_locator'] and ev['status'] == 'fact'
    assert ans['Sauna Beta']['verdict'] == 'no'
    assert {x['name']: x for x in q_rapid_cold_foreigner_day_pass(twin, date(2026, 11, 4))}['Sauna Alpha']['criteria']['open_today'] == 'closed'

def test_closure_rules():
    assert closed_on('closed 1st Wed monthly', date(2026, 11, 4)) is True
    assert closed_on('매주 월요일 휴무', date(2026, 10, 26)) is True
    assert closed_on('가을 정기휴장', date(2026, 10, 26)) is None

def test_daemon_retry_dead_letter_resume_and_cost_cap(env, tmp_path):
    a, twin, run, _ = env
    a.venue('alpha::', 'Sauna Alpha', URL)
    a.doc(URL, '2026-10-01T09:00:00+09:00', [('hours', {'hours': '06:00-22:00'}, 'fact', '06:00-22:00')], 'alpha::')
    _, state, stats = run(faults={'extract': 2})
    assert state == 'ok' and twin.execute("SELECT attempts,state FROM stage_failures").fetchone()[:] == (2, 'done')
    a.doc(URL, '2026-10-02T09:00:00+09:00', [('hours', {'hours': '07:00-22:00'}, 'fact', '07:00-22:00')], 'alpha::')
    _, state, stats = run(faults={'state_update': 3})
    assert state == 'failed' and 'recompute_views' not in stats
    assert len(hours_facts(twin)) == 1  # failed stage rolled back
    assert run()[1] == 'blocked'  # dead letter holds the pipeline at the failed stage
    twin.execute("UPDATE stage_failures SET state='done' WHERE state='dead'")  # operator re-queues
    assert run()[1] == 'ok' and len(hours_facts(twin)) == 2
    with pytest.raises(CostCapExceeded): charge(twin, 'r', 'extract', 1)
    with ProcessLock(tmp_path / 'twin' / 'lock'):
        with pytest.raises(AlreadyRunning): run()
