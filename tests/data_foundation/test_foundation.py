"""Offline contract, temporal integrity and real legacy acceptance tests."""
import copy
import csv
import datetime as dt
import importlib.util
import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from data_foundation.db import REPO, data_paths, external_path, open_db
from data_foundation.facts import insert_fact, observe, record as make_record, validate_fact
from data_foundation.legacy import EXCLUDED, BLOCKED_DOMAIN, LABELS, PHONE, EMAIL, import_legacy
from data_foundation.state import current_state, state_history, changed_since, match_known

FIXTURES=Path(__file__).parent/'fixtures'
T1='2026-10-20T00:00:00+09:00'
T2='2026-10-21T00:00:00+09:00'
T3='2026-10-22T00:00:00+09:00'


def record(*args, **kwargs):
    defaults = {'evidence_locator': {'section': 'Example policy'},
                'source_url': 'https://example.invalid/facts'}
    defaults.update(kwargs)
    return make_record(*args, **defaults)


@pytest.fixture
def db(tmp_path):
    conn=open_db(tmp_path/'facts.sqlite3')
    conn.execute("INSERT INTO entities VALUES ('ven_example','Venue',NULL,'{}')")
    conn.commit()
    yield conn
    conn.close()


def fact(**kwargs):
    return record('ven_example','Venue','tattoo','Allowed by example policy',value=True,status='verified_fact',collected_at=T1,**kwargs)


def test_ddl_contract(db):
    assert (REPO/'schema/fact_store.sql').read_bytes()==(REPO/'migrations/0001_fact_store.sql').read_bytes()
    db.executescript((REPO/'schema/fact_store.sql').read_text())
    cols={r['name'] for r in db.execute('PRAGMA table_info(facts)')}
    ontology=(REPO/'docs/data/ONTOLOGY.md').read_text()
    listed=set(re.findall(r'^- `([^`]+)`$',ontology,re.M))
    assert listed==cols
    assert set(json.loads((REPO/'schema/fact.schema.json').read_text())['properties'])==cols


def test_external_paths(tmp_path,monkeypatch):
    for path in (REPO/'unsafe.sqlite3', REPO, REPO/'raw'):
        with pytest.raises(ValueError):external_path(path)
    link=tmp_path/'repo';link.symlink_to(REPO,target_is_directory=True)
    with pytest.raises(ValueError):open_db(link/'unsafe.sqlite3')
    monkeypatch.setenv('HANSET_DATA_DIR',str(tmp_path/'external'))
    raw,dbpath=data_paths()
    assert raw==tmp_path/'external/raw'
    assert dbpath==tmp_path/'external/db/hanset_facts.sqlite3'
    monkeypatch.setenv('HANSET_DATA_DIR',str(REPO))
    with pytest.raises(ValueError):data_paths()


def test_fact_schema_and_formats():
    validate_fact(fact())
    for changes in ({'status':'unsupported'},{'evidence_locator':{}},{'collected_at':'yesterday'},{'collected_at':'2026-10-20T00:00:00Z'}, {'value_state':'unknown','status':'unverified','value':True},{'value':'no limit'},{'value':'unlimited'},{'value':''},{'value_text':'','value':None}):
        f=fact();f.update(changes)
        with pytest.raises((ValidationError,ValueError)):validate_fact(f)
    f=fact();f.update(valid_from=T2,valid_to=T1)
    with pytest.raises(ValueError):validate_fact(f)
    f=fact();f.update(value_state='unknown',status='unverified',value=None,value_text='Not confirmed')
    validate_fact(f)


def test_append_only(db):
    f=fact();assert insert_fact(db,f);db.commit()
    for sql in ("UPDATE facts SET value='false'", "UPDATE facts SET status='source_claim'",'DELETE FROM facts',"UPDATE facts SET closed_reason='changed'"):
        with pytest.raises(sqlite3.IntegrityError):db.execute(sql)
    db.execute('UPDATE facts SET valid_to=?,closed_reason=?',(T2,'changed'))
    with pytest.raises(sqlite3.IntegrityError):db.execute('UPDATE facts SET valid_to=?',(T3,))
    with pytest.raises(sqlite3.IntegrityError):db.execute('UPDATE facts SET valid_to=NULL')


def test_sql_rejects_bad_unknown_and_dates(db):
    f=fact();insert_fact(db,f)
    for assignments in ({'fact_id':'bad1','value_state':'unknown','status':'unverified','value':'true'}, {'fact_id':'bad2','valid_from':T3,'valid_to':T1}):
        raw=dict(db.execute('SELECT * FROM facts').fetchone());raw.update(assignments)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('INSERT INTO facts ('+','.join(raw)+') VALUES ('+','.join('?' for _ in raw)+')',tuple(raw.values()))


@pytest.mark.parametrize('kind,attribute,value1,value2',[
 ('OpeningHours','hours',{'opens':'06:00'},{'opens':'07:00'}),
 ('PriceTicket','amount',30000,35000),('Closure','maintenance',False,True),
 ('Bath','temperature',15,12),('SaunaRoom','availability',True,False),
 ('AccessCondition','tattoo',False,True),('Event','status','scheduled','cancelled')])
def test_all_tracked_state_flows(db,kind,attribute,value1,value2):
    db.execute('INSERT INTO entities VALUES (?,?,?,?)',('object_example',kind,'ven_example','{}'));db.commit()
    make=lambda value,stamp:record('object_example',kind,attribute,str(value),value=value,collected_at=stamp,status='verified_fact')
    a=make(value1,T1);assert observe(db,a)=='inserted'
    b=make(value1,T2);assert observe(db,b)=='confirmed'
    assert len(state_history(db,'object_example',attribute))==1
    assert db.execute('SELECT count(*) FROM fact_confirmations').fetchone()[0]==1
    for sql in ('DELETE FROM fact_confirmations',"UPDATE fact_confirmations SET run_id='other'"):
        with pytest.raises(sqlite3.IntegrityError):db.execute(sql)
    c=make(value2,T3);assert observe(db,c)=='changed'
    rows=current_state(db,'object_example');assert len(rows)==1 and rows[0]['value']==value2
    assert rows[0]['supersedes_fact_id']==a['fact_id']
    assert current_state(db,'object_example',T2)[0]['value']==value1
    assert len(state_history(db,'object_example',attribute))==2
    assert changed_since(db,T2)
    assert observe(db,c)=='duplicate'


def test_conflict_and_unknown(db):
    a=fact();observe(db,a)
    b=fact(source_url='https://example.invalid/other');b.update(value=False,value_text='Not permitted',collected_at=T2,created_at=T2,fact_id='fact_other')
    assert observe(db,b)=='conflict'
    current=current_state(db,'ven_example');assert len(current)==2
    assert {f['value'] for f in current}=={False,True}
    assert len({f['conflict_group'] for f in current})==1
    review=dict(db.execute('SELECT * FROM review_items').fetchone())
    Draft202012Validator(json.loads((REPO/'schema/review_item.schema.json').read_text()),format_checker=FormatChecker()).validate(review|{'fact_ids':json.loads(review['fact_ids'])})
    assert review['state']=='open'
    u=record('ven_example','Venue','tattoo','Not confirmed',value_state='unknown',status='unverified',collected_at=T3)
    assert observe(db,u)=='unknown_retained'
    current=current_state(db,'ven_example');assert len(current)==3
    result=match_known(current,lambda f:f['value'] is True)
    assert len(result['matches'])==1 and len(result['unknown'])==1
    assert len([f for f in current if f['value_state']=='known'])==2


def test_correction(db):
    a=fact();observe(db,a)
    b=record('ven_example','Venue','tattoo','Not permitted',value=False,status='verified_fact',collected_at=T2,correction_of=a['fact_id'])
    assert observe(db,b)=='changed'
    old=dict(db.execute('SELECT * FROM facts WHERE fact_id=?',(a['fact_id'],)).fetchone())
    assert old['closed_reason']=='corrected' and old['closed_by_fact_id']==b['fact_id']
    assert current_state(db,'ven_example')[0]['correction_of']==a['fact_id']


def test_registry_constraints():
    s=json.loads((REPO/'schema/source_registry.schema.json').read_text())
    v=Draft202012Validator(s,format_checker=FormatChecker())
    entry=json.loads((FIXTURES/'source_registry.json').read_text());v.validate(entry)
    for path,value in [('required',False),('status','enabled-bad'),('hold_reason',None),('pace.window_kst','all-day'),('pace.gap_min_seconds',0),('pace.daily_cap',999),('pace.bypass_allowed',True),('pace.writes_allowed',True),('pace.stop_on_challenge',False),('permission.authenticated',True),('permission.read_only',False)]:
        e=copy.deepcopy(entry);parts=path.split('.');d=e
        for p in parts[:-1]:d=d[p]
        d[parts[-1]]=value
        with pytest.raises(ValidationError):v.validate(e)


def test_fixture_migration(tmp_path):
    target=tmp_path/'facts.sqlite3';root=FIXTURES/'legacy'
    report=import_legacy(target,root)
    assert report['by_status']['verified_fact']>0
    assert report['by_status']['source_claim']>0
    assert report['by_status']['unverified']>0
    assert report['by_source_type']['venue_reply']>0
    db=open_db(target)
    assert db.execute("SELECT count(*) FROM facts WHERE attribute='price' AND value_state='unknown'").fetchone()[0]>0
    assert db.execute("SELECT valid_to FROM facts WHERE attribute='closure' AND valid_to IS NOT NULL").fetchone()[0]=='2026-11-12T23:59:59.999999+09:00'
    assert db.execute("SELECT count(*) FROM facts WHERE attribute='korean_phone_required' AND value_state='unknown'").fetchone()[0]==1
    assert db.execute("SELECT count(*) FROM facts WHERE value_state='not_applicable'").fetchone()[0]==1
    assert all(r[0] is None for r in db.execute("SELECT source_url FROM facts WHERE source_type='venue_reply'"))
    db.close()
    second=import_legacy(target,root)
    assert second['inserted']==0 and not second['by_entity_type'] and not second['by_status']
    dry=tmp_path/'dry.sqlite3';planned=import_legacy(dry,root,dry_run=True)
    assert planned['inserted']==report['inserted']
    db=open_db(dry);assert db.execute('SELECT count(*) FROM facts').fetchone()[0]==0;db.close()


def test_exclusions_and_excluded_domain(tmp_path):
    root=tmp_path/'input';shutil.copytree(FIXTURES/'legacy',root)
    p=root/'site/content/venues.json';v=json.loads(p.read_text())
    c=copy.deepcopy(v['Example Hotel Sauna']);c['slug']='excluded-example'
    # Policy test data is generated in temp storage, never kept in fixtures.
    v[EXCLUDED[0]]=c;p.write_text(json.dumps(v))
    sauna=root/'saunas.csv'
    with sauna.open() as f:reader=csv.DictReader(f);fields=reader.fieldnames;rows=list(reader)
    rows[0]['source_url']='https://'+BLOCKED_DOMAIN+'.com/example'
    with sauna.open('w') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
    report=import_legacy(tmp_path/'facts.sqlite3',root)
    assert report['skipped']==1 and report['blocked_source_rows']>0
    db=open_db(tmp_path/'facts.sqlite3')
    assert not db.execute("SELECT 1 FROM facts WHERE source_url LIKE ?",('%'+BLOCKED_DOMAIN+'%',)).fetchone()
    assert not db.execute("SELECT 1 FROM facts WHERE source_url IS NULL AND status='verified_fact'").fetchone()
    db.close()


def test_real_file_acceptance(tmp_path):
    path=tmp_path/'real.sqlite3';report=import_legacy(path)
    venues=json.loads((REPO/'site/content/venues.json').read_text())
    assert report['migrated_venues']+report['skipped']==sum(not n.startswith('_') for n in venues)
    assert all(report['by_status'].get(k,0)>0 for k in ('verified_fact','source_claim','unverified'))
    db=open_db(path)
    for row in db.execute('SELECT * FROM facts'):
        assert json.loads(row['evidence_locator']) and row['collected_at']
        assert row['status']!='unverified' or row['value'] is None
        assert row['status']!='verified_fact' or row['source_url']
        assert row['status']!='verified_fact' or not re.search(r'unverified|third-party|clue|not official|editor|비공식|미확인|instagram|reply|회신|당근|blog|operator',row['value_text'],re.I)
        assert not PHONE.search(row['value_text'])
        assert not any(t.lower() in row['value_text'].lower() for t in EXCLUDED)
    labels={label for n,c in venues.items() if not n.startswith('_') for label,text in c['facts']}
    assert labels <= LABELS.keys()
    db.close()
    assert import_legacy(path)['inserted']==0


def test_document_fixture_safety():
    paths=[* (REPO/'docs/data').rglob('*'),* (REPO/'schema').rglob('*'),* FIXTURES.rglob('*'),REPO/'migrations/README.md']
    for path in paths:
        if not path.is_file():continue
        for line in path.read_text().splitlines():
            assert not PHONE.search(line),path
            assert not any(t.lower() in line.lower() for t in EXCLUDED),path
            if BLOCKED_DOMAIN in line.lower():
                assert line.strip()=='Naver is not a source.',path
            assert all(m=='hanset@soulbound.co.kr' for m in EMAIL.findall(line)),path


def test_confirmation_other_source_and_replay(db):
    first=fact();observe(db,first)
    repeat=fact(source_url='https://example.invalid/second')
    repeat.update(fact_id='fact_recheck',collected_at=T2,created_at=T2)
    assert observe(db,repeat)=='confirmed'
    assert observe(db,repeat)=='confirmed'
    assert len(state_history(db,'ven_example','tattoo'))==1
    confirmation=dict(db.execute('SELECT * FROM fact_confirmations').fetchone())
    assert confirmation['source_url']==repeat['source_url']
    assert db.execute('SELECT count(*) FROM fact_confirmations').fetchone()[0]==1


def test_qualifiers_keep_distinct_ticket_scopes(db):
    weekday=fact();weekday.update(attribute='amount',value=30000,value_text='Weekday ticket',qualifiers={'day_type':'weekday'})
    weekend=fact(source_url='https://example.invalid/second')
    weekend.update(fact_id='fact_weekend',attribute='amount',value=40000,value_text='Weekend ticket',qualifiers={'day_type':'weekend'})
    assert observe(db,weekday)=='inserted'
    assert observe(db,weekend)=='inserted'
    assert len(current_state(db,'ven_example'))==2
    assert db.execute('SELECT count(*) FROM review_items').fetchone()[0]==0


def test_ai_output_cannot_promote_itself():
    f=fact();f.update(source_type='ai_extraction',status='source_claim')
    with pytest.raises(ValidationError):validate_fact(f)
    f['status']='ai_inference';validate_fact(f)


def test_invalid_observation_rolls_back_history(db):
    original=fact();observe(db,original)
    incoming=record('ven_example','Venue','tattoo','Changed policy',value=False,collected_at=T2,valid_from='2026-10-19T00:00:00+09:00')
    # Unknown start is not evidence of an earlier state; an incoming change cannot
    # end a known-start interval before that interval starts.
    db.execute('INSERT INTO entities VALUES (?,?,?,?)',('test_state','AccessCondition','ven_example','{}'));db.commit()
    a=record('test_state','AccessCondition','tattoo','Allowed',value=True,collected_at=T1,valid_from=T1)
    observe(db,a)
    incoming.update(subject_id='test_state',subject_type='AccessCondition')
    with pytest.raises(sqlite3.IntegrityError):observe(db,incoming)
    assert len(current_state(db,'test_state'))==1
    assert len(state_history(db,'test_state','tattoo'))==1


def test_explicit_effective_periods_and_null_unknown(tmp_path):
    root=tmp_path/'input';shutil.copytree(FIXTURES/'legacy',root)
    p=root/'site/content/venues.json';v=json.loads(p.read_text())
    v['Example Hotel Sauna']['facts'][0]=['요금(2026.11.01~11.30)','{price}']
    p.write_text(json.dumps(v))
    p=root/'site/content/foreigner.json';f=json.loads(p.read_text())
    f['example-hotel']['passport']=None
    f['example-hotel']['card']='  Not listed on official page'
    p.write_text(json.dumps(f))
    target=tmp_path/'facts.sqlite3';import_legacy(target,root)
    db=open_db(target)
    dated=dict(db.execute("SELECT * FROM facts WHERE valid_from IS NOT NULL LIMIT 1").fetchone())
    assert dated['valid_from']=='2026-11-01T00:00:00+09:00'
    assert dated['valid_to']=='2026-11-30T23:59:59.999999+09:00'
    assert db.execute("SELECT value_state FROM facts WHERE attribute='passport'").fetchone()[0]=='unknown'
    assert db.execute("SELECT value_state FROM facts WHERE attribute='card'").fetchone()[0]=='unknown'
    db.close()


def test_operational_lease_and_budget_constraints(db):
    acquire='''INSERT INTO run_locks(stage,owner,heartbeat,expires_at) VALUES (?,?,?,?)
    ON CONFLICT(stage) DO UPDATE SET owner=excluded.owner,heartbeat=excluded.heartbeat,
    expires_at=excluded.expires_at WHERE julianday(run_locks.expires_at)<=julianday(?)'''
    assert db.execute(acquire,('extract','worker_a',T1,T2,T1)).rowcount==1
    assert db.execute(acquire,('extract','worker_b',T1,T3,T1)).rowcount==0
    assert db.execute(acquire,('extract','worker_b',T2,T3,T2)).rowcount==1
    assert db.execute('SELECT owner FROM run_locks').fetchone()[0]=='worker_b'
    db.execute('INSERT INTO budgets VALUES (?,?,?,?,?,?,?,?,?,?)',('run','example',1,10,0,0,1,10,0,0))
    with pytest.raises(sqlite3.IntegrityError):db.execute('UPDATE budgets SET requests=2')
    with pytest.raises(sqlite3.IntegrityError):db.execute('UPDATE budgets SET bytes=-1')


@pytest.mark.parametrize('text,channel', [
    ('Hotel reply mentions Instagram roundup', 'venue_reply'),
    ('Third-party Instagram roundup', 'third_party'),
    ('Not official Instagram profile', 'third_party'),
    ('비공식 Instagram', 'third_party'),
    ('미확인 단서 Instagram', 'third_party'),
    ('Unverified clue on Instagram', 'third_party'),
    ('Blog Instagram mention', 'third_party'),
    ('Operator profile on Instagram', 'operator_profile'),
    ('Daangn profile', 'operator_profile'),
    ('당근 프로필', 'operator_profile'),
    ('Official Instagram @example', 'official_social'),
    ('DM confirmation', 'official_social'),
    ('Replying and administration hours', 'official_page'),
])
def test_channel_precedence(text, channel):
    from data_foundation.legacy import classify
    assert classify(text,'high official','https://example.invalid')[1]==channel
    assert classify('Official hours','verified Instagram reply operator','https://example.invalid')[1]=='legacy_site_content'


@pytest.mark.parametrize('channel,text,url,website,keep', [
    ('official_page','Official page','https://example.invalid/page',None,True),
    ('official_social','Instagram @example','https://www.instagram.com/example/',None,True),
    ('official_social','Instagram @other','https://instagram.com/example/',None,False),
    ('official_social','Instagram @example','https://example.invalid',None,False),
    ('operator_profile','Daangn profile','https://www.daangn.com/profile',None,True),
    ('operator_profile','Daangn profile','https://instagram.com/example',None,False),
    ('third_party','Blog','https://example.invalid/post','https://example.invalid',False),
    ('third_party','Blog','https://other.invalid/post','https://example.invalid',True),
    ('venue_reply','Reply','https://example.invalid',None,False),
])
def test_row_url_attribution(channel,text,url,website,keep):
    from data_foundation.legacy import attributed_url
    assert attributed_url(channel,text,url,website)==(url if keep else None)


@pytest.mark.parametrize('attribute', ['price','full_price','scrub_fee','guest_discount','last_entry'])
@pytest.mark.parametrize('text', ['Sauna admission fee not listed on official page.', 'Entry not stated.', 'Fee not shown.', 'Fee not on the official page.'])
def test_absence_guard(attribute,text):
    f=record('ven_example','Venue',attribute,text)
    with pytest.raises(ValueError):validate_fact(f)
    f.update(value_state='unknown',status='unverified',value=None)
    validate_fact(f)


def test_migration_absence_provenance_and_state(tmp_path):
    root=tmp_path/'input';shutil.copytree(FIXTURES/'legacy',root)
    p=root/'site/content/foreigner.json';data=json.loads(p.read_text())
    data['example-hotel'].update(full_price='Sauna admission fee not listed on official page.',
        scrub_fee='Admission fee not listed · Body scrub ₩35,000 extra',
        last_entry='Last entry not stated.', guest_discount='Free admission included; fee not listed')
    p.write_text(json.dumps(data))
    p=root/'site/content/venues.json';data=json.loads(p.read_text())
    data['Example Hotel Sauna']['tips'] += ['Official Instagram @example profile', 'Venue-run Daangn profile', 'Third-party Instagram roundup']
    p.write_text(json.dumps(data))
    target=tmp_path/'facts.sqlite3';report=import_legacy(target,root);db=open_db(target)
    for attr in ('full_price','last_entry'):
        row=db.execute('SELECT * FROM facts WHERE attribute=?',(attr,)).fetchone()
        assert row['value_state']=='unknown' and row['status']=='unverified' and row['value'] is None
    row=db.execute("SELECT * FROM facts WHERE attribute='scrub_fee'").fetchone()
    assert row['value_state']=='known' and row['status']=='source_claim'
    assert json.loads(row['evidence_locator'])['mixed_absence_clause'] is True
    row=db.execute("SELECT * FROM facts WHERE attribute='guest_discount' AND value_text LIKE 'Free%'").fetchone()
    assert row['value_state']=='known'
    for row in db.execute("SELECT * FROM facts WHERE attribute='tip' AND value_text LIKE '%profile%'"):
        assert row['source_url'] is None and row['status']=='source_claim'
        evidence=json.loads(row['evidence_locator'])
        assert evidence['venue_level_source_url'] and evidence['source_scope']=='venue_level_legacy'
    assert current_state(db,'closure_example-reply')
    assert state_history(db,'closure_example-reply','closure')
    hours=[f for f in current_state(db,'hours_example-hotel') if f['attribute']=='opening_hours']
    assert {f['qualifiers']['language'] for f in hours}=={'ko','en'}
    assert len({f['qualifiers']['label'] for f in hours})==1
    old=next(f for f in hours if f['qualifiers']['language']=='ko')
    new=copy.deepcopy(old);new.update(fact_id='fact_changed_hours',value={'opens':'07:00','closes':'21:00'},value_text='07:00–21:00',collected_at=T3,created_at=T3)
    assert observe(db,new)=='changed'
    history=state_history(db,'hours_example-hotel','opening_hours')
    prior=next(f for f in history if f['fact_id']==old['fact_id'])
    assert prior['valid_to']==T3 and prior['value_text']==old['value_text']
    assert any(f['fact_id']==new['fact_id'] for f in current_state(db,'hours_example-hotel'))
    assert sum(report['by_language'].values())==report['inserted']
    assert 0<report['distinct_fact_keys']<report['inserted']
    db.close()
    assert import_legacy(target,root)['inserted']==0


@pytest.mark.parametrize('channel',['official_social','operator_profile'])
def test_null_channel_url_contract(db,channel):
    f=fact();f.update(source_type=channel,source_url=None,status='source_claim')
    insert_fact(db,f)
    f.update(status='verified_fact',fact_id='fact_invalid')
    with pytest.raises(ValidationError):validate_fact(f)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE facts SET status='verified_fact'")


@pytest.mark.parametrize('text', ['Not listed; scrub fee 35,000 KRW', 'Not stated; free admission included', 'Admission included free; not shown on page'])
def test_absence_mixed_or_free_is_known(text):
    validate_fact(record('ven_example','Venue','price',text))


@pytest.mark.parametrize('text,expected', [
    ('Not listed; body scrub 35,000 KRW','source_claim'),
    ('Not stated; free admission included','verified_fact'),
])
def test_migration_absence_prefix_exceptions(tmp_path,text,expected):
    root=tmp_path/'input';shutil.copytree(FIXTURES/'legacy',root)
    p=root/'site/content/foreigner.json';data=json.loads(p.read_text())
    data['example-hotel']['full_price']=text;p.write_text(json.dumps(data))
    target=tmp_path/'facts.sqlite3';import_legacy(target,root);db=open_db(target)
    row=db.execute("SELECT * FROM facts WHERE attribute='full_price' AND value_text=?",(text,)).fetchone()
    assert row['value_state']=='known' and row['status']==expected
    db.close()
