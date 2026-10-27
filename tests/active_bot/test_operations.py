"""Operational interfaces, with no remote requests."""
import json
from datetime import datetime, timezone
import pytest
from hanset_active import cli
from hanset_active.connectors.instagram import InstagramConnector, ConnectorPaused
from hanset_active.locks import RunLock, AlreadyRunning
from hanset_active.report import duplicate_check, report, status, export_service_data
from hanset_active.runner import recover, run, fail_task
from hanset_active.scheduler import enqueue
from hanset_active.util.timeutil import FakeClock


def test_locks_and_heartbeat(bot):
    with RunLock(bot.db,bot.settings.data_dir,'first',clock=bot.clock) as lock:
        before=bot.db.execute('SELECT heartbeat FROM run_lease').fetchone()[0]
        bot.clock.sleep(5)
        lock.heartbeat()
        assert bot.db.execute('SELECT heartbeat FROM run_lease').fetchone()[0]>before
        with pytest.raises(AlreadyRunning):
            with RunLock(bot.db,bot.settings.data_dir,'second'): pass
        assert run(bot,initialize=False)['state']=='already_running'
    assert not bot.db.execute('SELECT * FROM run_lease').fetchall()


def test_ig_conditions(monkeypatch):
    clock=FakeClock(datetime(2026,1,1,4,0,tzinfo=timezone.utc))
    c=InstagramConnector('/not-opened',clock=clock)
    with pytest.raises(ConnectorPaused): c.read_profile('example')
    monkeypatch.setenv('HANSET_IG_ENABLED','1')
    assert c.read_profile('example') is None
    with pytest.raises(ConnectorPaused): InstagramConnector(clock=clock).read_profile('example')
    clock.sleep(3600)
    with pytest.raises(ConnectorPaused): c.read_profile('example')


def test_recover_and_three_attempts(bot):
    tid=enqueue(bot.db,kind='fetch',url='https://venue-a.test')
    with bot.db: bot.db.execute("UPDATE tasks SET state='running',lease_until=? WHERE task_id=?",(bot.clock.now().isoformat(),tid))
    recover(bot.db,clock=bot.clock)
    assert bot.db.execute('SELECT state FROM tasks').fetchone()[0]=='pending'
    for attempt,delay in enumerate((600,3600,21600),1):
        task=dict(bot.db.execute('SELECT * FROM tasks').fetchone())
        fail_task(bot.db,task,RuntimeError(),clock=bot.clock)
        row=bot.db.execute('SELECT * FROM tasks').fetchone()
        assert row['attempts']==attempt
        assert row['state']==('dead' if attempt==3 else 'failed')
        bot.clock.sleep(delay)
        recover(bot.db,clock=bot.clock)
    assert bot.db.execute('SELECT state FROM failures').fetchone()[0]=='dead'


def test_reports_and_export(bot,tmp_path):
    assert duplicate_check(bot.db) and not any(duplicate_check(bot.db).values())
    assert report(bot.db)['duplicate_check']['total']==0
    assert status(bot.db)['latest_run'] is None
    paths=export_service_data(bot.db,tmp_path/'out')
    assert len(paths)==6 and json.loads(open(paths[0]).read())==[]
    from hanset_active.config import REPO_ROOT
    with pytest.raises(ValueError): export_service_data(bot.db,REPO_ROOT/'bad')


def test_cli_all_commands(tmp_path,capsys):
    base=['--data-dir',str(tmp_path/'external')]
    for command in (['init'],['seed'],['status'],['report','--json'],['tasks'],['gaps','--venue','none'],['failures'],['ig-stub-check'],['run','--dry-run'],['export-service-data','--out',str(tmp_path/'export')]):
        assert cli.main(base+command)==0
        assert json.loads(capsys.readouterr().out) is not None
    assert cli.main(base+['run','--classes','invalid'])==2
    capsys.readouterr()


def test_dry_run_and_classes(bot):
    tid=enqueue(bot.db,kind='search',payload={'query':'sauna'},task_class='freshness')
    before=bot.db.total_changes
    assert run(bot,dry_run=True,classes='discover')['tasks']==[]
    assert run(bot,initialize=False,classes='discover',providers=[])['completed']==0
    assert bot.db.execute('SELECT state FROM tasks WHERE task_id=?',(tid,)).fetchone()[0]=='pending'


def test_duplicate_check_counts_corruption(bot):
    # A damaged/imported database may lack a UNIQUE constraint. The report
    # must detect duplicates itself rather than merely trusting the schema.
    with bot.db:
        bot.db.execute('DROP TABLE venue_links')
        bot.db.execute('CREATE TABLE venue_links(venue_id TEXT,url TEXT,kind TEXT)')
        bot.db.executemany('INSERT INTO venue_links VALUES (?,?,?)',[('venue','https://venue-a.test','sns')]*3)
    assert duplicate_check(bot.db)['venue_links']==2
    assert report(bot.db)['duplicate_check']['total']==2


def test_module_entrypoint(tmp_path):
    import subprocess
    import sys
    result=subprocess.run([sys.executable,'-m','hanset_active','--data-dir',str(tmp_path/'external'),'init'],capture_output=True,text=True,timeout=10)
    assert result.returncode==0
    assert json.loads(result.stdout)['state']=='initialized'


def test_invalid_seed_website_and_clock_tick_changes(bot):
    from hanset_active.venues import seed,upsert_venue,apply_observation
    from hanset_active.extract.fields import extract
    from hanset_active.labeling import store_observation
    seed(bot.db,[{'name':'Known Sauna','website':'http://[invalid'}],clock=bot.clock)
    assert bot.db.execute('SELECT website FROM seed_venues').fetchone()[0]==''
    vid=upsert_venue(bot.db,'Aurora Sauna',website='https://venue-a.test',clock=bot.clock)
    key=bot.db.execute('SELECT venue_key FROM venues WHERE venue_id=?',(vid,)).fetchone()[0]
    for amount,route in (('25000','a'),('31000','b'),('25000','c')):
        oid=store_observation(bot.db,key,extract('Day pass '+amount+' KRW')[0],source_url='https://venue-a.test/'+route,source_type='own_site',clock=bot.clock)
        apply_observation(bot.db,vid,oid,clock=bot.clock)
    assert bot.db.execute('SELECT count(*) FROM venue_fields').fetchone()[0]==3
    assert bot.db.execute('SELECT count(*) FROM field_changes').fetchone()[0]==2
