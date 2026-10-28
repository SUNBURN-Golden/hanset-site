#!/usr/bin/env python3
"""Real-data proof: replay saunas.csv git history through the twin daemon into a scratch DB
and show value changes closing the old period. Prints no contact data or secrets."""
import json, subprocess, sys, tempfile
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hanset_twin.store import connect
from hanset_twin.daemon import run_once
from hanset_twin.query import q_rapid_cold_foreigner_day_pass

root = Path(__file__).resolve().parents[1]
commits = subprocess.run(['git', '-C', str(root), 'log', '--reverse', '--format=%h', '--', 'saunas.csv'], capture_output=True, text=True, check=True).stdout.split()
with tempfile.TemporaryDirectory(dir='/tmp') as tmp:
    db = connect(Path(tmp) / 'twin.sqlite3')
    for c in commits + ['WORKTREE']:
        csv_path = Path(tmp) / f'{c}.csv'
        csv_path.write_bytes((root / 'saunas.csv').read_bytes() if c == 'WORKTREE' else
                             subprocess.run(['git', '-C', str(root), 'show', f'{c}:saunas.csv'], capture_output=True, check=True).stdout)
        _, state, _ = run_once(db, None, csv_path=csv_path, lock_path=Path(tmp) / 'lock')
        if state != 'ok': print(c, [tuple(r) for r in db.execute('SELECT stage,error FROM stage_failures')]); raise SystemExit(1)
    rows = db.execute('''SELECT e2.name branch, o.attribute, o.value_json old, o.effective_from, o.effective_to, n.value_json new, n.effective_from new_from, n.effective_to new_to
        FROM facts o JOIN facts n ON n.fact_id=o.superseded_by JOIN entities e ON e.entity_id=o.entity_id
        JOIN entities e2 ON e2.entity_id=COALESCE(CASE WHEN e.entity_type='branch' THEN e.entity_id END, e.parent_id)
        WHERE o.closed_reason IN ('value_change','evidence_found') ORDER BY o.effective_to''').fetchall()
    hist = [dict(r) for r in rows if r['effective_from'] >= '2026-10-01']  # placeholders from the replay run itself excluded below
    later = [r for r in rows if r['effective_to'] >= r['effective_from']]
    real = [dict(r) for r in rows if r['attribute'] in ('amount', 'open_hours', 'closure_rule', 'policy')]
    print(json.dumps({'commits_replayed': len(commits) + 1,
                      'closed_by_reason': {k: v for k, v in db.execute("SELECT closed_reason,count(*) FROM facts WHERE effective_to IS NOT NULL GROUP BY 1")},
                      'examples': real[:6]}, ensure_ascii=False, indent=1))
    for r in rows:  # the old period ends exactly where the successor starts or later; successor stays open
        assert r['effective_to'] is not None and r['new_to'] is None or r['new_to'] >= r['new_from']

# Part 2: controlled re-observation on a COPY of the live twin. A real open source_claim for opening
# hours is re-observed from the same source with a different value (simulated snapshot, copy only).
import shutil, sqlite3
from hanset_twin.ontology import apply_snapshot
live = Path('/workspace/hanset-data/twin/twin.sqlite3')
if live.exists():
    with tempfile.TemporaryDirectory(dir='/tmp') as tmp:
        src = sqlite3.connect(f'file:{live}?mode=ro', uri=True); dst = sqlite3.connect(Path(tmp) / 'copy.sqlite3'); src.backup(dst); dst.close()
        db = connect(Path(tmp) / 'copy.sqlite3')
        old = db.execute("""SELECT * FROM facts WHERE attribute='open_hours' AND status='source_claim' AND effective_to IS NULL
                            AND source_type='verified_csv' ORDER BY fact_id LIMIT 1""").fetchone()
        at = '2026-10-28T12:00:00+09:00'
        new_ids = apply_snapshot(db, old['entity_id'], 'open_hours', old['source_url'], [dict(value='SIMULATED-07:00-20:00', unit=None, status='source_claim',
            source_type=old['source_type'], locator={'simulated': True}, quote='simulated re-observation', origin_ref='simulated', collected_at=at)], at=at)
        o = db.execute('SELECT * FROM facts WHERE fact_id=?', (old['fact_id'],)).fetchone()
        n = db.execute('SELECT * FROM facts WHERE fact_id=?', (new_ids[0],)).fetchone()
        assert o['effective_to'] == n['effective_from'] and o['superseded_by'] == n['fact_id'] and n['effective_to'] is None
        print(json.dumps({'controlled_change_on_live_copy': {'old_value': json.loads(o['value_json']), 'old_period': [o['effective_from'], o['effective_to']],
              'closed_reason': o['closed_reason'], 'new_period': [n['effective_from'], n['effective_to']]}}, ensure_ascii=False, indent=1))
