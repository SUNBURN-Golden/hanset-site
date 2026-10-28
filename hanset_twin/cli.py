import argparse, json, os, sys, uuid
from datetime import date
from pathlib import Path
from hanset_active.config import REPO_ROOT
from hanset_active.util.timeutil import kst_now
from .store import connect, open_active_readonly, DEFAULT_DIR, DEFAULT_ACTIVE
from .daemon import run_once, AlreadyRunning, ProcessLock
from .stages import Ctx, reextract as reextract_stage
from . import report

def main(argv=None):
    p = argparse.ArgumentParser(prog='hanset_twin')
    p.add_argument('cmd', choices=['run', 'report', 'ask', 'reextract'])
    p.add_argument('--twin-dir', default=str(DEFAULT_DIR))
    p.add_argument('--active-db', default=str(DEFAULT_ACTIVE))
    p.add_argument('--csv', default=str(REPO_ROOT / 'saunas.csv'))
    p.add_argument('--date', default=None)
    a = p.parse_args(argv)
    db = connect(Path(a.twin_dir) / 'twin.sqlite3')
    if a.cmd == 'run':
        active = open_active_readonly(a.active_db) if Path(a.active_db).exists() else None
        try:
            run_id, state, stats = run_once(db, active, csv_path=a.csv, lock_path=Path(a.twin_dir) / 'daemon.lock')
        except AlreadyRunning:
            print('already_running'); return 0
        print(json.dumps({'run_id': run_id, 'state': state, 'stats': stats}))
        return 0 if state == 'ok' else 1
    if a.cmd == 'reextract':
        if not Path(a.csv).exists():
            print(f'csv_not_found: {a.csv}', file=sys.stderr); return 1
        active = open_active_readonly(a.active_db) if Path(a.active_db).exists() else None
        run_id = uuid.uuid4().hex
        try:
            with ProcessLock(Path(a.twin_dir) / 'daemon.lock'):
                db.execute('INSERT INTO daemon_runs VALUES (?,?,NULL,?,NULL)',
                           (run_id, kst_now().isoformat(), 'running'))
                result = reextract_stage(Ctx(db, active, csv_path=a.csv, run_id=run_id))
                db.execute("UPDATE daemon_runs SET finished_at=?, state='ok', stats=? WHERE run_id=?",
                           (kst_now().isoformat(), json.dumps(result), run_id))
        except AlreadyRunning:
            print('already_running'); return 0
        except Exception as exc:
            db.execute("UPDATE daemon_runs SET finished_at=?, state='failed', stats=? WHERE run_id=?",
                       (kst_now().isoformat(), json.dumps({'error': str(exc)}), run_id))
            raise
        print(json.dumps(result))
        return 0
    if a.cmd == 'report':
        print(json.dumps(report.summary(db, date.fromisoformat(a.date) if a.date else date.today()), ensure_ascii=False, indent=1)); return 0
    from .query import q_rapid_cold_foreigner_day_pass
    print(json.dumps(q_rapid_cold_foreigner_day_pass(db, date.fromisoformat(a.date) if a.date else date.today()), ensure_ascii=False, indent=1)); return 0
