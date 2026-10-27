import pytest
from hanset_active.gaps import ensure_gaps, resolve, advance, record_attempt, reopen
from hanset_active.venues import upsert_venue
from hanset_active.labeling import store_observation
from hanset_active.extract.fields import extract

@pytest.fixture
def gap(bot):
    vid=upsert_venue(bot.db,'Silver Sauna',website='https://silver.example')
    gaps=ensure_gaps(bot.db,vid,clock=bot.clock)
    assert len(gaps)==9
    assert len(ensure_gaps(bot.db,vid))==9
    return next(g for g in gaps if g['field']=='price_day_pass')

def observe(bot,gap,price=25000,url='https://silver.example/price',source='own_site'):
    venue=bot.db.execute('SELECT * FROM venues WHERE venue_id=?',(gap['venue_id'],)).fetchone()
    return store_observation(bot.db,venue['venue_key'],extract(f'Day pass {price} KRW')[0],source_url=url,source_type=source,clock=bot.clock)

def test_filled_and_conflict(bot,gap):
    observe(bot,gap,source='third_party')
    assert resolve(bot.db,gap['gap_id'])=='open'
    observe(bot,gap,url="https://silver.example/official")
    assert resolve(bot.db,gap['gap_id'])=='filled'
    assert bot.db.execute('SELECT evidence_url FROM gaps WHERE gap_id=?',(gap['gap_id'],)).fetchone()[0]=='https://silver.example/official'
    observe(bot,gap,27000,'https://silver.example/other')
    assert resolve(bot.db,gap['gap_id'])=='conflict'
    resolve(bot.db,gap['gap_id'])
    assert bot.db.execute('SELECT count(*) FROM review_queue').fetchone()[0]==1

@pytest.mark.parametrize('pages,expected',[
 ([{'url':'https://silver.example/price','text':'Day pass 25000','outcome':'ok'}],'not_extracted'),
 ([{'url':'https://silver.example/price','outcome':'blocked'}],'inaccessible'),
 ([{'url':f'https://silver.example/{n}','text':'No mention here','outcome':'ok','counterpart':n==1,'attachment':n==2} for n in range(3)],'absent_in_source'),
 ([{'url':'https://silver.example/image','outcome':'ok','unextracted_attachment':True}],'not_extracted')])
def test_final_states(bot,gap,pages,expected):
    assert resolve(bot.db,gap['gap_id'],pages,exhausted=True)==expected

def test_strategies_followup_and_reopen(bot,gap):
    gid=gap['gap_id']
    for strategy in ('site_links','lang_counterpart','attachments','images'):
        assert advance(bot.db,gid)==[]
        assert bot.db.execute('SELECT strategy FROM gap_attempts ORDER BY rowid DESC LIMIT 1').fetchone()[0]==strategy
    tasks=advance(bot.db,gid)
    assert tasks and bot.db.execute('SELECT kind FROM tasks WHERE task_id=?',(tasks[0],)).fetchone()[0]=='search'
    assert advance(bot.db,gid)==[]
    row=bot.db.execute('SELECT * FROM gap_attempts WHERE task_id=?',(tasks[0],)).fetchone()
    record_attempt(bot.db,gid,'search',url=row['url'],task_id=tasks[0])
    assert advance(bot.db,gid)
    assert not reopen(bot.db,gid,clock=bot.clock)
    bot.db.execute("UPDATE gaps SET next_at='2025-01-01',state='inaccessible' WHERE gap_id=?",(gid,));bot.db.commit()
    assert reopen(bot.db,gid,clock=bot.clock)
    assert bot.db.execute('SELECT count(*) FROM gap_attempts').fetchone()[0]==0

def test_box_fields(bot):
    vid=upsert_venue(bot.db,'Silver CrossFit',venue_type='crossfit_box')
    assert len(ensure_gaps(bot.db,vid))==4
