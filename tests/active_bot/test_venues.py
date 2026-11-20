import json

import pytest

from hanset_active.venues import seed, is_article_title, is_new, upsert_venue, apply_observation, normalize_name
from hanset_active.labeling import store_observation
from hanset_active.extract.fields import extract

def test_seed_freeze(bot):
    db=bot.db
    sid=seed(db,[{'name':'Known Sauna','website':'https://known.example'}])
    assert sid==seed(db,[{'name':'Other','website':'https://other.example'}])
    assert not is_new(db,'Known Sauna','https://new.example')
    assert not is_new(db,'Different','https://known.example')
    assert is_new(db,'Other','https://other.example')
    seed(db,[{'name':'Other','website':'https://other.example'}],refreeze=True,clock=bot.clock)
    assert not is_new(db,'Other','https://other.example')
    assert is_new(db,'Known Sauna','https://known.example')

def test_empty_seed_freeze(bot):
    assert seed(bot.db)==seed(bot.db,[{'name':'Late Sauna'}])
    assert is_new(bot.db,'Late Sauna')


@pytest.mark.parametrize('name', [
    'Seed Spa A 추천', 'Seed Spa A ＢＥＳＴ', 'Seed Spa A top １０', 'Seed Spa A 어디',
    'Seed Spa A 총정리', 'Seed Spa A 후기', 'Seed Spa A？', 'Seed Spa A!',
    '  ３ 곳 Seed Spa A', '5선 Box B', 'Seed Spa A ' * 4 + '후기',
])
def test_article_title_rejected_before_venue_alias_or_seed_writes(bot, name):
    db = bot.db
    site = 'https://example.test/visit'
    assert is_article_title(name)
    assert upsert_venue(db, 'Seed Spa A', website=site)
    assert upsert_venue(db, 'Box B')
    before = list(db.iterdump())
    assert upsert_venue(db, name, website=site) is None
    assert upsert_venue(db, name) is None
    assert list(db.iterdump()) == before
    seed(db, [{'name': name, 'website': site}, {'name': 'Box B'}])
    assert [row['name'] for row in db.execute('SELECT name FROM seed_venues')] == ['Box B']
    assert db.execute('SELECT count(*) FROM seed_domains').fetchone()[0] == 0


@pytest.mark.parametrize('name', ['Seed Spa A', 'Box B', 'Seed Spa A Wellness and Recreation Center',
                                 'Bestwood Sauna', 'Top Floor Sauna', '24 Hours Spa'])
def test_venue_name_without_article_markers_is_kept(bot, name):
    assert not is_article_title(name)
    assert upsert_venue(bot.db, name)

def test_identity_and_review(bot):
    a=upsert_venue(bot.db,'Silver Sauna',website='https://silver.example',city='Seoul')
    assert a==upsert_venue(bot.db,'Silver Sauna',website='https://else.example',city='Seoul')
    assert a==upsert_venue(bot.db,'Other name',website='https://silver.example',city='Seoul')
    b=upsert_venue(bot.db,'Silver Sauna East',website='https://east.example',city='Busan')
    assert b!=a
    assert bot.db.execute("SELECT count(*) FROM review_queue WHERE kind='possible_duplicate'").fetchone()[0]==1

def test_history_noop(bot):
    db=bot.db;vid=upsert_venue(db,'Silver Sauna',website='https://silver.example')
    key=db.execute('SELECT venue_key FROM venues').fetchone()[0]
    def observe(price):
        return store_observation(db,key,extract(f'Day pass {price} KRW')[0],source_url='https://silver.example/price',source_type='own_site',clock=bot.clock)
    a=observe(25000);first=apply_observation(db,vid,a,clock=bot.clock)
    assert apply_observation(db,vid,a,clock=bot.clock)==first
    bot.clock.sleep(60);b=observe(27000)
    apply_observation(db,vid,b,clock=bot.clock);apply_observation(db,vid,b,clock=bot.clock)
    rows=db.execute('SELECT * FROM venue_fields ORDER BY valid_from').fetchall()
    assert len(rows)==2 and rows[0]['valid_to'] and rows[1]['valid_to'] is None
    change=db.execute('SELECT * FROM field_changes').fetchone()
    assert json.loads(change['old_value'])['amount']==25000
    assert json.loads(change['new_value'])['amount']==27000
    assert db.execute('SELECT count(*) FROM field_changes').fetchone()[0]==1

def test_replaying_old_observation_preserves_new_current(bot):
    db=bot.db;vid=upsert_venue(db,'Silver Sauna',website='https://silver.example')
    key=db.execute('SELECT venue_key FROM venues').fetchone()[0]
    old=store_observation(db,key,extract('Day pass 25000 KRW')[0],source_url='https://silver.example/old',source_type='own_site',clock=bot.clock)
    first=apply_observation(db,vid,old,clock=bot.clock)
    bot.clock.sleep(60)
    new=store_observation(db,key,extract('Day pass 27000 KRW')[0],source_url='https://silver.example/new',source_type='own_site',clock=bot.clock)
    current=apply_observation(db,vid,new,clock=bot.clock)
    assert apply_observation(db,vid,old,clock=bot.clock)==first
    assert db.execute('SELECT vf_id FROM venue_fields WHERE valid_to IS NULL').fetchone()[0]==current
    assert db.execute('SELECT count(*) FROM field_changes').fetchone()[0]==1
