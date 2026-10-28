import argparse, json, os, sys
from datetime import date
from pathlib import Path
from hanset_active.config import REPO_ROOT
from .store import connect, open_active_readonly, DEFAULT_DIR, DEFAULT_ACTIVE
from .daemon import run_once, AlreadyRunning
from . import report

def main(argv=None):
    p = argparse.ArgumentParser(prog='hanset_twin')
    p.add_argument('cmd', choices=['run', 'report', 'ask'])
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
    if a.cmd == 'report':
        print(json.dumps(report.summary(db, date.fromisoformat(a.date) if a.date else date.today()), ensure_ascii=False, indent=1)); return 0
    from .query import q_rapid_cold_foreigner_day_pass
    print(json.dumps(q_rapid_cold_foreigner_day_pass(db, date.fromisoformat(a.date) if a.date else date.today()), ensure_ascii=False, indent=1)); return 0
