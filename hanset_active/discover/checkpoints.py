"""Durable discovery cursors in the active store."""
import json

from ..db import deterministic_id
from ..util.timeutil import kst_now


def load(db, adapter, dataset):
    key = deterministic_id('seeds', adapter, dataset)
    row = db.execute('SELECT cursor FROM run_checkpoints WHERE checkpoint_id=?', (key,)).fetchone()
    return json.loads(row[0]) if row else {}


def save(db, adapter, dataset, cursor, clock):
    value = json.dumps(cursor, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    db.execute('''INSERT INTO run_checkpoints(checkpoint_id,kind,cursor,updated_at) VALUES (?,?,?,?)
                  ON CONFLICT(checkpoint_id) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at
                  WHERE run_checkpoints.cursor!=excluded.cursor''',
               (deterministic_id('seeds', adapter, dataset), 'seeds:' + adapter, value, kst_now(clock).isoformat()))
