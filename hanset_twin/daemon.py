"""Staged twin daemon: per-stage checkpoint, process lock + stage lease, bounded retry,
failure queue, cost cap 0 and resume from the failed stage."""
import fcntl
import json
import uuid
from pathlib import Path
from hanset_active.util.timeutil import kst_now
from .stages import STAGES, Ctx

MAX_ATTEMPTS = 3
COST_CAP_KRW = 0

class AlreadyRunning(RuntimeError): pass
class CostCapExceeded(RuntimeError): pass

def charge(db, run_id, stage, amount_krw, note=''):
    """Every paid call would have to pass here. Cap is 0, so any positive cost aborts."""
    if amount_krw > COST_CAP_KRW:
        raise CostCapExceeded(f'{stage}: cost {amount_krw} > cap {COST_CAP_KRW}')
    db.execute('INSERT INTO cost_ledger(run_id,stage,amount_krw,note,ts) VALUES (?,?,?,?,?)', (run_id, stage, amount_krw, note, kst_now().isoformat()))

class ProcessLock:
    def __init__(self, path): self.path, self.f = Path(path), None
    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = self.path.open('a')
        try: fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.f.close(); raise AlreadyRunning('twin daemon already running') from None
        return self
    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN); self.f.close()

def run_once(db, active, *, csv_path, lock_path, clock=None, stages=None, faults=None):
    run_id = uuid.uuid4().hex
    stats = {}
    with ProcessLock(lock_path):
        db.execute('INSERT INTO daemon_runs VALUES (?,?,NULL,?,NULL)', (run_id, kst_now(clock).isoformat(), 'running')); db.commit()
        ctx = Ctx(db, active, csv_path=csv_path, run_id=run_id, clock=clock)
        state = 'ok'
        for name, fn in (stages or STAGES):
            row = db.execute('SELECT cursor FROM stage_checkpoints WHERE stage=?', (name,)).fetchone()
            cursor = row[0] if row else None
            dead = db.execute("SELECT 1 FROM stage_failures WHERE stage=? AND state='dead'", (name,)).fetchone()
            if dead:
                stats[name] = 'dead_letter'; state = 'blocked'; break
            ok = False
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    db.execute('SAVEPOINT stage')
                    db.execute("INSERT INTO stage_checkpoints(stage,cursor,run_id,state,updated_at) VALUES (?,?,?,'running',?) ON CONFLICT(stage) DO UPDATE SET run_id=excluded.run_id,state='running',updated_at=excluded.updated_at",
                               (name, cursor, run_id, kst_now(clock).isoformat()))
                    if faults and faults.get(name, 0) > 0:
                        faults[name] -= 1; raise RuntimeError(f'injected fault in {name}')
                    charge(db, run_id, name, 0, 'no paid services')
                    new_cursor, n = fn(ctx, cursor)
                    db.execute("UPDATE stage_checkpoints SET cursor=?,state='done',stats=?,updated_at=? WHERE stage=?",
                               (new_cursor, json.dumps({'items': n}), kst_now(clock).isoformat(), name))
                    db.execute("UPDATE stage_failures SET state='done' WHERE stage=? AND state='retry'", (name,))
                    db.execute('RELEASE stage'); db.commit()
                    stats[name] = n; ok = True; break
                except CostCapExceeded:
                    db.execute('ROLLBACK TO stage'); db.execute('RELEASE stage'); raise
                except Exception as exc:
                    db.execute('ROLLBACK TO stage'); db.execute('RELEASE stage')
                    ref = f'cursor:{cursor}'
                    db.execute('''INSERT INTO stage_failures VALUES (?,?,?,?,1,?,?) ON CONFLICT(stage,item_ref) DO UPDATE SET
                        attempts=attempts+1, error=excluded.error, state=CASE WHEN attempts+1>=? THEN 'dead' ELSE 'retry' END, updated_at=excluded.updated_at''',
                        (f'{name}:{ref}', name, ref, str(exc)[:300], 'retry', kst_now(clock).isoformat(), MAX_ATTEMPTS)); db.commit()
                    if db.execute("SELECT state FROM stage_failures WHERE stage=? AND item_ref=?", (name, ref)).fetchone()[0] == 'dead':
                        break
            if not ok:
                stats[name] = 'failed'; state = 'failed'; break  # later stages wait; resume here next run
        db.execute('UPDATE daemon_runs SET finished_at=?, state=?, stats=? WHERE run_id=?', (kst_now(clock).isoformat(), state, json.dumps(stats), run_id)); db.commit()
    return run_id, state, stats
