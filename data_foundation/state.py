"""Historical and current state, retaining unknowns and conflicting sources."""
import json


def decode(row):
    d = dict(row)
    for key in ('value','qualifiers','evidence_locator'):
        d[key] = json.loads(d[key]) if d[key] is not None else None
    return d


def current_state(db, subject, as_of=None):
    if as_of is None:
        rows = db.execute('SELECT * FROM current_facts WHERE subject_id=?',(subject,))
    else:
        rows = db.execute("SELECT * FROM facts WHERE subject_id=? AND julianday(COALESCE(valid_from,collected_at))<=julianday(?) AND (valid_to IS NULL OR julianday(valid_to)>julianday(?))",(subject,as_of,as_of))
    return [decode(r) for r in rows]


def state_history(db, subject, attribute):
    return [decode(r) for r in db.execute('SELECT * FROM facts WHERE subject_id=? AND attribute=? ORDER BY collected_at,fact_id',(subject,attribute))]


def changed_since(db, ts):
    return [decode(r) for r in db.execute('SELECT * FROM facts WHERE julianday(created_at)>julianday(?) OR julianday(valid_to)>julianday(?)',(ts,ts))]


def match_known(facts, predicate):
    """Unknowns are exposed separately, never treated as policy permission."""
    return {'matches':[f for f in facts if f['value_state']=='known' and predicate(f)], 'unknown':[f for f in facts if f['value_state']=='unknown']}
