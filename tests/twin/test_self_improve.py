"""Focused tests for the 2026-10-29 self-improvement: day-pass classification, wider CSV text
extraction, answer hygiene (exclusions) and the reextract maintenance command.
All venues, URLs and values are synthetic and de-identified."""
import json
from datetime import date
import pytest
from hanset_active.db import connect as active_connect, deterministic_id
from hanset_twin.store import connect, open_active_readonly
from hanset_twin.daemon import run_once
from hanset_twin import stages
from hanset_twin.stages import classify_price_note, reextract, Ctx
from hanset_twin.query import q_rapid_cold_foreigner_day_pass, slot, q1_excluded
from hanset_twin import report
from hanset_twin.cli import main as cli_main

CSV_HEAD = 'name,name_ko,type,city,district,website,price_krw,price_note,notable,source_url,confidence,last_verified,notes,verification_status,verification_source_url,verification_date,verification_detail,official_contact\n'

def csv_line(name, *, name_ko='', type_='boutique sauna', website='', price='', note='', notable='', notes='', status=''):
    source = f'https://{name.split()[0].lower()}-demo.example/spa'
    return ','.join([name, name_ko, type_, 'Seoul', '', website, price, f'"{note}"', f'"{notable}"',
                     source, '', '2026-10-01', f'"{notes}"', status, '', '', '', '']) + '\n'

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
        from html import escape
        title = self.db.execute('SELECT name FROM venues WHERE venue_key=?', (venue_key,)).fetchone()
        blob = self.path.parent / (raw + '.html')
        blob.write_text('<title>' + escape(title[0] if title else 'Example Page') + '</title><main>' +
                        ''.join('<p>' + escape(o[3]) + '</p>' for o in obs) + '</main>')
        self.db.execute('UPDATE raw_documents SET blob_path=?, content_type=? WHERE raw_id=?', (str(blob), 'text/html', raw))
        for i, (field, value, label, quote) in enumerate(obs):
            self.db.execute('''INSERT INTO observations(obs_id,venue_key,field,value_json,value_text,value_hash,label_status,source_url,source_type,quote,locator,raw_id,observed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''', (deterministic_id(raw, i), venue_key, field, json.dumps(value), str(value), deterministic_id(value),
                label, url, source_type, quote, json.dumps({'char_offsets': [i, i + 5]}), raw, fetched_at))
        self.db.commit()

@pytest.fixture
def env(tmp_path):
    a = Active(tmp_path / 'active.sqlite3')
    csv = tmp_path / 'saunas.csv'; csv.write_text(CSV_HEAD)
    twin = connect(tmp_path / 'twin' / 'twin.sqlite3')
    def run(**kw):
        return run_once(twin, open_active_readonly(a.path), csv_path=csv, lock_path=tmp_path / 'twin' / 'lock', **kw)
    def load(rows):
        csv.write_text(CSV_HEAD + ''.join(rows))
        return csv
    return a, twin, run, load, csv, tmp_path

def branch_id(twin, name):
    return twin.execute("SELECT entity_id FROM entities WHERE entity_type='branch' AND name=?", (name,)).fetchone()[0]

def open_facts(twin, bid, etype, kind, attribute):
    return twin.execute('''SELECT f.* FROM facts f JOIN entities e USING(entity_id) WHERE (e.entity_id=? OR e.parent_id=?)
        AND e.entity_type=? AND e.kind=? AND f.attribute=? AND f.effective_to IS NULL AND f.value_json IS NOT NULL''',
        (bid, bid, etype, kind, attribute)).fetchall()

# --- 1. deterministic price-note classifier ---------------------------------

@pytest.mark.parametrize('note,amount,ticket,quals', [
    ('Official: 사우나 정상가 1인 ₩55,000 (tax incl.)', 55000, 'day_pass', {}),
    ('Official: 일반객 ₩26,000; guests 50% off sauna price', 26000, 'day_pass', {}),
    ('공식: 성인 ₩24,000 (투숙객 ₩18,000) / 소인 ₩14,000', 24000, 'day_pass', {'audience': 'adult'}),
    ('풀앤스파 종일권 대인 ₩49,000(중학생 이상)', 49000, 'day_pass', {'audience': 'adult'}),
    ('일반(중학생 이상) 주중 ₩15,000·토/일/공휴일 ₩18,000', 15000, 'day_pass', {'scope': 'weekday'}),
    ('대인 주중/평일·주말 — 사우나 ₩15,000/₩18,000', 15000, 'day_pass', {'scope': 'weekday', 'audience': 'adult'}),
    ('day (05-23) ₩28,000, night (23-05) ₩35,000. Men only.', 28000, 'day_pass', {}),
    ("women's room 1 person 60 min ₩45,000~", 45000, 'day_pass', {'duration_hours': 1}),
    ('entry from ₩23,000 (10h)', 23000, 'day_pass', {'duration_hours': 10}),
    ('day pass ₩20,000; monthly all-access ₩250,000', 20000, 'day_pass', {}),
    ('adult from ₩10,000, senior ₩8,000, child ₩5,000', 10000, 'day_pass', {'audience': 'adult'}),
    # hotel-guest-only / add-on / guests-only note -> guest_rate
    ('adult ₩15,000 / child ₩10,000 ... guests only', 15000, 'guest_rate', {}),
    ('Guest Rate ₩35,000', 35000, 'guest_rate', {}),
    ('sauna 1 person add-on ₩44,000 incl. 10% VAT', 44000, 'guest_rate', {}),
    ('1x ₩44,000 (guests-only rate)', 44000, 'guest_rate', {}),
    ('투숙객 이용 요금 ₩35,000 (정상가 50% 할인)', 35000, 'guest_rate', {}),
    ('공식: 성인 ₩24,000 (투숙객 ₩18,000)', 18000, 'guest_rate', {}),
    # spa course / membership / trial -> stays listed_price
    ('시수 스파 베이직 ₩79,000 / 시수 스파 딥 ₩89,000', 79000, 'listed_price', {}),
    ('membership fee ₩100,000/월', 100000, 'listed_price', {}),
    ('첫 구매 체험권 ₩69,900 (1인 1회, 15일)', 69900, 'listed_price', {}),
    # amount absent from the note, or no admission cue next to it -> never day_pass
    ('Official: 사우나 정상가 1인 ₩55,000', 99000, 'listed_price', {}),
    ('', 55000, 'listed_price', {}),
    ('Not confirmed', 55000, 'listed_price', {}),
])
def test_classify_price_note(note, amount, ticket, quals):
    assert classify_price_note(note, amount) == (ticket, quals)

def test_classify_price_note_rejects_bad_amount():
    assert classify_price_note('성인 ₩15,000', None) == ('listed_price', {})
    assert classify_price_note('성인 ₩15,000', 'x') == ('listed_price', {})

# --- CSV prices flow into Q1 --------------------------------------------------

def test_csv_price_becomes_day_pass_visible_to_q1(env):
    a, twin, run, load, csv, _ = env
    load([csv_line('Terma Spa Demo', type_='large spa', price='15000', note='일반 주중 ₩15,000',
                   notable='급냉탕 운영; 매주 월요일 휴무', notes='외국인 이용 가능')])
    assert run()[1] == 'ok'
    bid = branch_id(twin, 'Terma Spa Demo')
    facts = open_facts(twin, bid, 'price_plan', 'day_pass', 'amount')
    assert len(facts) == 1 and facts[0]['status'] == 'source_claim'  # CSV stays source_claim, never fact
    assert json.loads(facts[0]['value_json']) == 15000
    assert json.loads(twin.execute('SELECT qualifiers FROM entities WHERE entity_id=?', (facts[0]['entity_id'],)).fetchone()[0]) == {'ticket': 'day_pass', 'scope': 'weekday'}
    ans = q_rapid_cold_foreigner_day_pass(twin, date(2026, 10, 27))[0]  # Tuesday: not the Monday closure
    assert ans['name'] == 'Terma Spa Demo'
    assert ans['criteria']['day_pass'] == 'yes'
    assert ans['criteria']['rapid_cold_plunge'] == 'yes'
    assert ans['criteria']['foreigner_allowed'] == 'yes'
    assert ans['criteria']['open_today'] == 'open'
    assert ans['criteria']['non_guest_access'] == 'unverified'  # access never assumed
    assert ans['verdict'] == 'unknown' and ans['answered_criteria'] == 4

def test_guest_rate_and_guests_only_hotel_answers_no(env):
    a, twin, run, load, csv, _ = env
    load([csv_line('Guest Hotel Demo', type_='hotel sauna (guests only)', price='35000', note='Guest Rate ₩35,000',
                   notable='hot/cold baths')])
    assert run()[1] == 'ok'
    bid = branch_id(twin, 'Guest Hotel Demo')
    assert [json.loads(r['value_json']) for r in open_facts(twin, bid, 'price_plan', 'guest_rate', 'amount')] == \
        [35000]
    assert not open_facts(twin, bid, 'price_plan', 'day_pass', 'amount')  # placeholder stays unverified
    ans = q_rapid_cold_foreigner_day_pass(twin, date(2026, 10, 27))[0]
    assert ans['criteria']['non_guest_access'] == 'no'  # guests-only access = no day pass for visitors
    assert ans['criteria']['day_pass'] == 'unverified'  # guest_rate is not a day pass
    assert ans['verdict'] == 'no'

def test_spa_course_price_stays_listed(env):
    a, twin, run, load, csv, _ = env
    load([csv_line('Sisu Demo House', price='79000', note='시수 스파 베이직 ₩79,000 / 시수 스파 딥 ₩89,000')])
    assert run()[1] == 'ok'
    bid = branch_id(twin, 'Sisu Demo House')
    assert [json.loads(r['value_json']) for r in open_facts(twin, bid, 'price_plan', 'listed_price', 'amount')] == [79000]
    assert q_rapid_cold_foreigner_day_pass(twin, date(2026, 10, 27))[0]['criteria']['day_pass'] == 'unverified'

# --- 2. wider CSV text extraction + hardened rules ----------------------------

def test_notes_and_price_note_columns_are_extracted(env):
    a, twin, run, load, csv, _ = env
    load([csv_line('Riverside Demo', price='28000', note='day (05-23) ₩28,000. Tattoos not admitted.',
                   notable='Event, open-air and hot baths', notes='외국인 이용 가능')])
    assert run()[1] == 'ok'
    bid = branch_id(twin, 'Riverside Demo')
    tattoo = open_facts(twin, bid, 'usage_condition', 'tattoo', 'policy')
    assert [json.loads(r['value_json']) for r in tattoo] == ['not_allowed']
    assert 'Tattoos not admitted' in tattoo[0]['quote']
    assert json.loads(open_facts(twin, bid, 'usage_condition', 'foreigner', 'policy')[0]['value_json']) == 'accepted'

def test_not_confirmed_boilerplate_yields_nothing(env):
    a, twin, run, load, csv, _ = env
    load([csv_line('Bong Demo Spring', notable='Large hot spring. Prices/day pass/tattoo/foreigner rules Not confirmed. 전화번호 게시하지 않음.',
                   notes='Prices/day pass/tattoo/foreigner rules Not confirmed.')])
    assert run()[1] == 'ok'
    bid = branch_id(twin, 'Bong Demo Spring')
    for etype, kind, attr in (('usage_condition', 'tattoo', 'policy'), ('usage_condition', 'foreigner', 'policy')):
        assert slot(twin, bid, etype, kind, attr)['state'] == 'unverified'
    assert twin.execute("SELECT count(*) FROM facts WHERE value_json IS NOT NULL").fetchone()[0] == 0
    assert twin.execute("SELECT count(*) FROM review_queue WHERE kind='ambiguous_extraction'").fetchone()[0] == 0

@pytest.mark.parametrize('notes,policy', [
    ('Tattoos not admitted.', 'not_allowed'),
    ('문신·피부병 환자 출입금지', 'not_allowed'),
    ('tattoos 15x15cm or larger permitted only when covered with a patch or personal rash guard', 'cover_required'),
    ('tattoo >15cm needs patch', 'cover_required'),
    ('visitors with tattoos may be restricted', 'conditional'),
])
def test_tattoo_policies_exactly_as_stated(env, notes, policy):
    a, twin, run, load, csv, _ = env
    load([csv_line('Tattoo Demo Spa', notes=notes)])
    assert run()[1] == 'ok'
    bid = branch_id(twin, 'Tattoo Demo Spa')
    assert [json.loads(r['value_json']) for r in open_facts(twin, bid, 'usage_condition', 'tattoo', 'policy')] == [policy]

@pytest.mark.parametrize('notable,exists', [
    ('Dry and wet saunas. Four hot baths, one cold', True),
    ('hot/cold baths, wet+dry sauna', True),
    ('cold plunge and meditation room', True),
    ('건식·습식 사우나·온탕/열탕·냉탕', True),
    ('14°C cold shower, ritual program', False),  # a cold shower is not a cold plunge
    ('Six large baths', False),
])
def test_cold_plunge_only_when_stated(env, notable, exists):
    a, twin, run, load, csv, _ = env
    load([csv_line('Cold Demo Bath', notable=notable)])
    assert run()[1] == 'ok'
    bid = branch_id(twin, 'Cold Demo Bath')
    rows = open_facts(twin, bid, 'facility', 'cold_plunge', 'exists')
    assert [json.loads(r['value_json']) for r in rows] == ([True] if exists else [])
    assert slot(twin, bid, 'facility', 'rapid_cold_plunge', 'exists')['state'] == 'unverified'  # never inferred

def test_dry_wet_sauna_exists_from_english_note(env):
    a, twin, run, load, csv, _ = env
    load([csv_line('Novo Demo Sauna', notable='Finnish dry and wet saunas. Four hot baths, one cold')])
    assert run()[1] == 'ok'
    bid = branch_id(twin, 'Novo Demo Sauna')
    assert json.loads(open_facts(twin, bid, 'facility', 'dry_sauna', 'exists')[0]['value_json']) is True
    assert json.loads(open_facts(twin, bid, 'facility', 'wet_sauna', 'exists')[0]['value_json']) is True

# --- 3. answer hygiene ---------------------------------------------------------

def test_runport_never_becomes_a_branch(env):
    a, twin, run, load, csv, _ = env
    load([csv_line('Terma Spa Demo', price='15000', note='일반 주중 ₩15,000'),
          csv_line('RUNPORT (Demo Clubhouse)', name_ko='런포트 데모', website='https://www.rundemo.example',
                   price='69900', note='첫 구매 체험권 ₩69,900 (1인 1회, 15일)', status='confirmed')])
    a.venue('rundemo::', 'RUNPORT Demo', 'https://www.rundemo.example')
    assert run()[1] == 'ok'
    names = [r[0] for r in twin.execute("SELECT name FROM entities WHERE entity_type='branch'")]
    assert names == ['Terma Spa Demo']  # owner-excluded venue: no branch from CSV or active store
    assert twin.execute("SELECT count(*) FROM candidate_branches WHERE name LIKE '%RUNPORT%'").fetchone()[0] == 0
    assert twin.execute("SELECT count(*) FROM work_items WHERE canonical_url LIKE '%rundemo%'").fetchone()[0] == 0

def test_q1_excludes_non_sauna_suspects_and_reports_them(env):
    a, twin, run, load, csv, _ = env
    load([csv_line('Terma Spa Demo', notable='급냉탕 운영')])
    a.venue('box::', 'Crossfit Box Demo', 'https://crossfit-demo.example')
    a.doc('https://crossfit-demo.example', '2026-10-01T09:00:00+09:00',
          [('hours', {'hours': '06:00-22:00'}, 'fact', '06:00-22:00')], 'box::')
    assert run()[1] == 'ok'
    assert twin.execute("SELECT count(*) FROM quality_issues WHERE check_id='non_sauna_branch_suspect' AND state='open'").fetchone()[0] == 1
    q = q_rapid_cold_foreigner_day_pass(twin, date(2026, 10, 27))
    assert [x['name'] for x in q] == ['Terma Spa Demo']  # suspect branch removed from the candidate list
    assert [x['name'] for x in q1_excluded(twin)] == ['Crossfit Box Demo']
    summary = report.summary(twin, date(2026, 10, 27))
    assert summary['q1']['branches'] == 1 and summary['q1_excluded_branches'] == ['Crossfit Box Demo']

def test_open_today_ignores_maintenance_flagged_hours(env):
    a, twin, run, load, csv, _ = env
    load([csv_line('Empty Demo Row')])
    a.venue('maint::', 'Sauna Maint Demo', 'https://maint-demo.example')
    a.doc('https://maint-demo.example', '2026-10-01T09:00:00+09:00',
          [('hours', {'hours': '09:00-10:00'}, 'fact', 'maintenance 9-10')], 'maint::')
    a.venue('allday::', 'Sauna Allday Demo', 'https://allday-demo.example')
    a.doc('https://allday-demo.example', '2026-10-01T09:00:00+09:00',
          [('hours', {'hours': '24_hours'}, 'fact', 'open 24 hours')], 'allday::')
    assert run()[1] == 'ok'
    assert twin.execute("SELECT count(*) FROM candidates WHERE verdict_reason='maintenance_window'").fetchone()[0] == 1
    ans = {x['name']: x['criteria']['open_today'] for x in q_rapid_cold_foreigner_day_pass(twin, date(2026, 10, 27))}
    assert ans['Sauna Maint Demo'] == 'unverified'  # flagged hours are the only hours -> ignored
    assert ans['Sauna Allday Demo'] == 'open'       # explicit 24h hours count

# --- 4. reextract ---------------------------------------------------------------

def test_reextract_reclassifies_prices_without_data_loss(env, monkeypatch):
    a, twin, run, load, csv, _ = env
    load([csv_line('Terma Spa Demo', price='15000', note='일반 주중 ₩15,000')])
    monkeypatch.setattr(stages, 'classify_price_note', lambda note, amount: ('listed_price', {}))
    assert run()[1] == 'ok'  # DB built under the old rules
    bid = branch_id(twin, 'Terma Spa Demo')
    old = open_facts(twin, bid, 'price_plan', 'listed_price', 'amount')
    assert len(old) == 1 and old[0]['effective_to'] is None
    monkeypatch.undo()  # new rules ship
    result = reextract(Ctx(twin, None, csv_path=csv, run_id='rex'))
    assert result['items'] == 1 and result['candidates'] >= 1
    closed = twin.execute('''SELECT f.* FROM facts f JOIN entities e USING(entity_id) WHERE e.parent_id=? AND e.kind='listed_price'
        AND f.attribute='amount' AND f.effective_to IS NOT NULL''', (bid,)).fetchall()
    new = open_facts(twin, bid, 'price_plan', 'day_pass', 'amount')
    assert len(closed) == 1 and closed[0]['closed_reason'] in ('value_change', 'evidence_removed')  # row kept, closed
    assert len(new) == 1 and json.loads(new[0]['value_json']) == 15000
    assert twin.execute("SELECT count(*) FROM corrections WHERE kind IN ('value_change','evidence_removed')").fetchone()[0] >= 1
    assert q_rapid_cold_foreigner_day_pass(twin, date(2026, 10, 27))[0]['criteria']['day_pass'] == 'yes'
    # idempotent: a second run changes nothing
    n_before = twin.execute('SELECT count(*), sum(effective_to IS NOT NULL) FROM facts').fetchone()
    reextract(Ctx(twin, None, csv_path=csv, run_id='rex2'))
    assert tuple(twin.execute('SELECT count(*), sum(effective_to IS NOT NULL) FROM facts').fetchone()) == tuple(n_before)

def test_reextract_closes_removed_values_as_evidence_removed(env, monkeypatch):
    a, twin, run, load, csv, _ = env
    load([csv_line('Tattoo Demo Spa', notes='Tattoos allowed but price not listed.')])
    from hanset_active.extract import fields as field_rules_module
    def old_loose_rule(text):  # what the pre-hardening extractor produced from this sentence
        if 'Tattoos allowed' in text:
            return [dict(field='tattoo_policy', value='allowed', unit=None, quote='Tattoos allowed',
                         locator={'char_offsets': [0, 14]}, ambiguous=False, rule_id='tattoo_policy.explicit.v1')]
        return []
    monkeypatch.setattr(field_rules_module, 'extract', old_loose_rule)
    assert run()[1] == 'ok'
    bid = branch_id(twin, 'Tattoo Demo Spa')
    assert [json.loads(r['value_json']) for r in open_facts(twin, bid, 'usage_condition', 'tattoo', 'policy')] == ['allowed']
    monkeypatch.undo()  # hardened rules: "not listed" boilerplate yields nothing
    reextract(Ctx(twin, None, csv_path=csv, run_id='rex'))
    closed = twin.execute('''SELECT f.* FROM facts f JOIN entities e USING(entity_id) WHERE e.parent_id=? AND e.kind='tattoo'
        AND f.attribute='policy' AND f.effective_to IS NOT NULL''', (bid,)).fetchall()
    assert len(closed) == 1 and closed[0]['closed_reason'] == 'evidence_removed' and closed[0]['superseded_by'] is None
    assert twin.execute("SELECT count(*) FROM corrections WHERE kind='evidence_removed'").fetchone()[0] == 1
    assert slot(twin, bid, 'usage_condition', 'tattoo', 'policy')['state'] == 'unverified'  # explicit gap again

def test_reextract_requires_the_csv(env, tmp_path):
    from hanset_twin.store import connect as twin_connect
    twin = twin_connect(tmp_path / 'twin2' / 'twin.sqlite3')
    with pytest.raises(FileNotFoundError):
        reextract(Ctx(twin, None, csv_path=tmp_path / 'nope.csv', run_id='rex'))

def test_reextract_cli(env, capsys, tmp_path):
    a, twin, run, load, csv, _ = env
    load([csv_line('Terma Spa Demo', price='15000', note='일반 주중 ₩15,000')])
    assert run()[1] == 'ok'
    rc = cli_main(['reextract', '--twin-dir', str(tmp_path / 'twin'), '--csv', str(csv),
                   '--active-db', str(tmp_path / 'missing.sqlite3')])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out['items'] == 1 and out['candidates'] >= 1
    assert cli_main(['reextract', '--twin-dir', str(tmp_path / 'twin'), '--csv', str(tmp_path / 'nope.csv'),
                     '--active-db', str(tmp_path / 'missing.sqlite3')]) == 1
    assert run()[1] == 'ok'  # daemon still runs normally afterwards (checkpoints untouched)

@pytest.mark.parametrize('note', ['adult ₩15,000 Not confirmed', '성인 ₩15,000 미확인', 'day pass ₩15,000 확인 안 됨'])
def test_unconfirmed_csv_price_has_no_value(env, note):
    _, twin, run, load, _, _ = env
    load([csv_line('Unconfirmed Demo', price='15000', note=note)])
    assert run()[1] == 'ok'
    assert twin.execute("SELECT count(*) FROM facts WHERE attribute='amount' AND value_json IS NOT NULL").fetchone()[0] == 0


def test_ambiguous_repeated_csv_price_goes_to_review(env):
    _, twin, run, load, _, _ = env
    load([csv_line('Ambiguous Demo', price='15000', note='adult ₩15,000; guest rate ₩15,000')])
    assert run()[1] == 'ok'
    assert twin.execute("SELECT count(*) FROM facts WHERE attribute='amount' AND value_json IS NOT NULL").fetchone()[0] == 0
    assert twin.execute("SELECT count(*) FROM review_queue WHERE kind='ambiguous_extraction'").fetchone()[0] == 1


def test_price_cue_does_not_cross_another_amount():
    assert classify_price_note('adult ₩15,000; locker ₩5,000', 5000) == ('listed_price', {})
    assert classify_price_note('spa course adult ₩15,000', 15000) == ('listed_price', {})


def test_reextract_is_atomic_on_failure(env, monkeypatch):
    _, twin, run, load, csv, _ = env
    load([csv_line('Atomic Demo', price='15000', note='일반 ₩15,000')])
    assert run()[1] == 'ok'
    before = [tuple(r) for r in twin.execute('SELECT * FROM candidates ORDER BY cand_id')]
    def fail(*args):
        raise RuntimeError('synthetic failure')
    monkeypatch.setattr(stages, 'evidence_verify', fail)
    with pytest.raises(RuntimeError, match='synthetic failure'):
        reextract(Ctx(twin, None, csv_path=csv, run_id='rex'))
    assert [tuple(r) for r in twin.execute('SELECT * FROM candidates ORDER BY cand_id')] == before


def test_reextract_does_not_replay_obsolete_csv_snapshots(env):
    _, twin, run, load, csv, _ = env
    load([csv_line('History Demo', price='15000', note='adult ₩15,000')])
    assert run()[1] == 'ok'
    load([csv_line('History Demo', price='18000', note='adult ₩18,000')])
    assert run()[1] == 'ok'
    bid = branch_id(twin, 'History Demo')
    reextract(Ctx(twin, None, csv_path=csv, run_id='rex'))
    assert [json.loads(r['value_json']) for r in open_facts(twin, bid, 'price_plan', 'day_pass', 'amount')] == [18000]
    before = [tuple(r) for r in twin.execute('SELECT fact_id,effective_to FROM facts ORDER BY fact_id')]
    reextract(Ctx(twin, None, csv_path=csv, run_id='rex2'))
    assert [tuple(r) for r in twin.execute('SELECT fact_id,effective_to FROM facts ORDER BY fact_id')] == before
