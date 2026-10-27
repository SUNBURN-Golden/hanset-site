import math
import pytest
from hanset_active.scheduler import Scheduler, enqueue, split_budget, score, urgency, refresh_days

@pytest.mark.parametrize('total',[0,1,4,10,100])
def test_split(total):
    shares=split_budget(total,{'discover':.01,'freshness':.98,'backfill':.01})
    assert sum(shares.values())==total
    assert shares['discover']>=math.ceil(.25*total)

def test_intervals_score():
    assert [refresh_days(f) for f in ('price_day_pass','hours','closures','tattoo_policy','identity')]==[3,3,3,14,30]
    assert urgency(6,'hours')==2
    assert refresh_days('hours',deprioritized=True)==30
    assert score({'novelty':1,'cost':1})==.25

def test_queue_and_cooldown(bot):
    db=bot.db
    a=enqueue(db,kind='fetch',url='https://one.example',novelty=1,clock=bot.clock)
    assert a==enqueue(db,kind='fetch',url='https://one.example',novelty=1,clock=bot.clock)
    b=enqueue(db,kind='fetch',url='https://two.example',clock=bot.clock)
    assert enqueue(db,kind='fetch',url='https://three.example',depth=3) is None
    scheduler=Scheduler(db,clock=bot.clock,budget=3)
    assert scheduler.pick(cooldowns={'one.example':5})['task_id']==b
    db.execute("UPDATE tasks SET state='done' WHERE task_id=?",(b,));db.commit()
    assert scheduler.pick(cooldowns={'one.example':5},deadline=6)['task_id']==a
    assert bot.clock.elapsed==5

def test_discover_reservation_and_flow(bot):
    for cls in ('freshness','discover'):
        enqueue(bot.db,kind=cls,url=f'https://{cls}.example',task_class=cls,urgency=100 if cls=='freshness' else 0)
    scheduler=Scheduler(bot.db,clock=bot.clock,budget=4)
    picked=[]
    for i in range(4): picked.append(scheduler.pick()['task_class'])
    assert 'discover' in picked

def test_floor_survives_discovery_cooldown(bot):
    enqueue(bot.db,kind='freshness',url='https://old.example',task_class='freshness',urgency=100)
    enqueue(bot.db,kind='search',url='https://new.example',task_class='discover')
    scheduler=Scheduler(bot.db,clock=bot.clock,budget=4)
    for _ in range(3):
        assert scheduler.pick(cooldowns={'new.example':10})['task_class']=='freshness'
    assert scheduler.pick(cooldowns={'new.example':10}) is None
    assert scheduler.pick(cooldowns={'new.example':10},deadline=11)['task_class']=='discover'

def test_future_task_wait_is_bounded(bot):
    from datetime import timedelta
    from hanset_active.util.timeutil import kst_now
    tid=enqueue(bot.db,kind='fetch',url='https://later.example')
    bot.db.execute('UPDATE tasks SET next_at=? WHERE task_id=?',((kst_now(bot.clock)+timedelta(seconds=8)).isoformat(),tid));bot.db.commit()
    scheduler=Scheduler(bot.db,clock=bot.clock)
    assert scheduler.pick(deadline=4) is None
    assert scheduler.pick(deadline=9)['task_id']==tid
    assert bot.clock.elapsed==8
