import sqlite3
import pytest
from hanset_active.db import connect, migrate, deterministic_id, consume_budget
from hanset_active.util.timeutil import FakeClock

def test_schema_and_idempotent_migration(tmp_path):
    db=connect(tmp_path/'test.db')
    migrate(db); migrate(db)
    assert db.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
    assert db.execute('PRAGMA foreign_keys').fetchone()[0]==1
    assert db.execute('SELECT count(*) FROM schema_version').fetchone()[0]==1
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set('raw_documents fetch_log extractions labels observations venues venue_aliases venue_links venue_fields field_changes confirmations seed_venues seed_domains sources source_trials tasks gaps gap_attempts review_queue failures runs run_checkpoints run_lease structure_baselines structure_alerts budgets_daily request_log robots_cache provider_state taxonomy_candidates'.split())<=tables
    db.close()

def test_uniqueness_and_foreign_keys():
    db=connect(':memory:')
    db.execute("INSERT INTO raw_documents(raw_id,canonical_url,content_hash) VALUES ('r','https://venue-a.test','hash')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO raw_documents(raw_id,canonical_url,content_hash) VALUES ('s','https://venue-a.test','hash')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO extractions(extraction_id,raw_id,extractor,extractor_version) VALUES ('e','missing','html','1')")
    db.execute("INSERT INTO tasks(task_id,dedupe_key) VALUES ('t','key')")
    with pytest.raises(sqlite3.IntegrityError): db.execute("INSERT INTO tasks(task_id,dedupe_key) VALUES ('u','key')")
    assert deterministic_id('a','bc')!=deterministic_id('ab','c')
    assert deterministic_id('a',{'x':1})==deterministic_id('a',{'x':1})
    db.close()

def test_atomic_daily_budget():
    db=connect(':memory:'); clock=FakeClock()
    assert consume_budget(db,'bodies',1,clock)
    assert not consume_budget(db,'bodies',1,clock)
    assert not consume_budget(db,'aux',0,clock)
    clock.sleep(86400)
    assert consume_budget(db,'bodies',1,clock)
    with pytest.raises(ValueError): consume_budget(db,'other',1,clock)
    db.close()
