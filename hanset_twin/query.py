"""Service views and representative-question queries. Only open fact/source_claim values answer;
'unverified' slots are reported as unknown, never guessed."""
import json
import re
from datetime import date, datetime

ANSWERING = ('fact', 'source_claim')
WEEKDAYS = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6,
            '월': 0, '화': 1, '수': 2, '목': 3, '금': 4, '토': 5, '일': 6}

def _facts(db, branch_id, etype=None, kind=None, attribute=None, at=None):
    sql = '''SELECT f.*, e.entity_type, e.kind, e.qualifiers FROM facts f JOIN entities e USING(entity_id)
             WHERE (e.entity_id=? OR e.parent_id=?)'''
    args = [branch_id, branch_id]
    if at:
        sql += ' AND f.effective_from<=? AND (f.effective_to IS NULL OR f.effective_to>?)'; args += [at, at]
    else:
        sql += ' AND f.effective_to IS NULL'
    for col, v in (('e.entity_type', etype), ('e.kind', kind), ('f.attribute', attribute)):
        if v is not None: sql += f' AND {col}=?'; args.append(v)
    return db.execute(sql, args).fetchall()

def evidence(row):
    return {'fact_id': row['fact_id'], 'value': json.loads(row['value_json']) if row['value_json'] else None, 'status': row['status'],
            'source_url': row['source_url'], 'evidence_locator': json.loads(row['evidence_locator']), 'quote': row['quote'],
            'collected_at': row['collected_at'], 'effective_from': row['effective_from'], 'effective_to': row['effective_to']}

def slot(db, branch_id, etype, kind, attribute, at=None):
    rows = _facts(db, branch_id, etype, kind, attribute, at)
    known = [r for r in rows if r['status'] in ANSWERING]
    values = {r['value_hash'] for r in known}
    if not known:
        return {'state': 'unverified', 'evidence': []}
    return {'state': 'conflict' if len(values) > 1 else 'known', 'evidence': [evidence(r) for r in known]}

def branch_view(db, branch_id):
    b = db.execute('SELECT * FROM entities WHERE entity_id=?', (branch_id,)).fetchone()
    view = {'branch_id': branch_id, 'name': b['name'], 'slots': {}}
    for r in _facts(db, branch_id):
        key = f"{r['entity_type']}.{r['kind'] or ''}.{r['attribute']}"
        q = json.loads(r['qualifiers'])
        if q and r['entity_type'] != 'branch': key += '.' + '.'.join(f'{k}={q[k]}' for k in sorted(q) if k != 'key')
        view['slots'].setdefault(key, []).append({'status': r['status'], **({'value': json.loads(r['value_json'])} if r['value_json'] else {}),
                                                  'source_url': r['source_url'], 'fact_id': r['fact_id']})
    return view

def _nth_weekday(d, n, wd):
    return d.weekday() == wd and (d.day - 1) // 7 + 1 == n

def closed_on(rule, d: date):
    """Return True/False if a closure rule text decides the date, None if it cannot be decided."""
    t = str(rule).lower()
    for a, b in re.findall(r'(\d{4}[-/]\d{2}[-/]\d{2})\s*[~–]\s*(\d{4}[-/]\d{2}[-/]\d{2})', t):
        if datetime.fromisoformat(a.replace('/', '-')).date() <= d <= datetime.fromisoformat(b.replace('/', '-')).date(): return True
    m = re.search(r'(?:매월\s*)?(첫째|둘째|셋째|넷째|1st|2nd|3rd|4th)\s*(?:주\s*)?([월화수목금토일]|mon|tue|wed|thu|fri|sat|sun)', t)
    if m:
        n = {'첫째': 1, '둘째': 2, '셋째': 3, '넷째': 4, '1st': 1, '2nd': 2, '3rd': 3, '4th': 4}[m[1]]
        return _nth_weekday(d, n, WEEKDAYS[m[2][:3]])
    m = re.search(r'매주\s*([월화수목금토일])|every\s*(mon|tue|wed|thu|fri|sat|sun)|closed (?:on )?(mon|tue|wed|thu|fri|sat|sun)', t)
    if m:
        return d.weekday() == WEEKDAYS[(m[1] or m[2] or m[3])[:3]]
    # "open every day" with no other closure wording decides the date as not closed
    if re.search(r'연중\s*무휴|open[_ ]every[_ ]day|open 365', t) and not re.search(r'휴관|휴장|(?<!무)휴무|closed|except|단[,\s]', t):
        return False
    return None

def _open_issue_refs(db, check_id):
    return {r[0] for r in db.execute('SELECT ref FROM quality_issues WHERE check_id=? AND state=?', (check_id, 'open'))}

def q1_excluded_branch_ids(db):
    """Branches with an open non_sauna_branch_suspect issue (e.g. CrossFit boxes typed sauna
    by the active bot): excluded from Q1's candidate list and reported separately."""
    return _open_issue_refs(db, 'non_sauna_branch_suspect')

def q1_excluded(db):
    rows = db.execute('''SELECT q.ref, e.name FROM quality_issues q LEFT JOIN entities e ON e.entity_id=q.ref
        WHERE q.check_id='non_sauna_branch_suspect' AND q.state='open' ORDER BY e.name''').fetchall()
    return [{'branch_id': r[0], 'name': r[1], 'reason': 'non_sauna_branch_suspect'} for r in rows]

def _scope_applies(scope, d):
    if scope in (None, '', 'unspecified'): return True
    if scope == 'weekday': return d.weekday() < 5
    if scope == 'weekend': return d.weekday() >= 5
    return True

def q_rapid_cold_foreigner_day_pass(db, today: date):
    """Q1: Which saunas have a rapid-cold plunge and let foreigners in on a day pass today?
    Branches flagged non_sauna_branch_suspect are excluded (see q1_excluded); hours facts
    flagged maintenance_window_as_hours never count towards open_today."""
    out = []
    excluded = q1_excluded_branch_ids(db)
    flagged_hours = _open_issue_refs(db, 'maintenance_window_as_hours')
    for (bid,) in db.execute("SELECT entity_id FROM entities WHERE entity_type='branch' ORDER BY name").fetchall():
        if bid in excluded:
            continue
        rapid = slot(db, bid, 'facility', 'rapid_cold_plunge', 'exists')
        foreign = slot(db, bid, 'usage_condition', 'foreigner', 'policy')
        access = slot(db, bid, 'usage_condition', 'access', 'policy')
        passes = [evidence(r) for r in _facts(db, bid, 'price_plan', None, 'amount') if r['status'] in ANSWERING and r['kind'] in ('day_pass', 'drop_in')]
        closures = [evidence(r) for r in _facts(db, bid, 'hours', 'closure', 'closure_rule') if r['status'] in ANSWERING and r['fact_id'] not in flagged_hours]
        regular = [r for r in _facts(db, bid, 'hours', 'regular', 'open_hours') if r['status'] in ANSWERING and r['fact_id'] not in flagged_hours]
        decided = [closed_on(c['value'], today) for c in closures]
        if closures:
            open_today = 'closed' if True in decided else ('open' if all(x is False for x in decided) else 'undecidable')
        elif regular:
            # explicit regular hours for a scope matching the date mean the venue operates on it
            operates = [r for r in regular if isinstance(json.loads(r['value_json']) if r['value_json'] else None, str)
                        and _scope_applies((json.loads(r['qualifiers']) or {}).get('scope'), today)]
            open_today = 'open' if operates else 'unverified'
        else:
            open_today = 'unverified'
        def yes(s, ok): return s['state'] == 'known' and all(ok(e['value']) for e in s['evidence'])
        criteria = {
            'rapid_cold_plunge': rapid['state'] if rapid['state'] != 'known' else ('yes' if yes(rapid, lambda v: v is True) else 'no'),
            'foreigner_allowed': foreign['state'] if foreign['state'] != 'known' else ('yes' if yes(foreign, lambda v: v in ('accepted', 'english_guide', 'id_required')) else 'no'),
            'non_guest_access': access['state'] if access['state'] != 'known' else ('no' if any(e['value'] in ('guests_only', 'members_and_guests') for e in access['evidence']) else 'yes'),
            'day_pass': 'yes' if passes else 'unverified',
            'open_today': open_today,
        }
        answered = sum(v not in ('unverified', 'undecidable', 'conflict') for v in criteria.values())
        verdict = 'no' if 'no' in criteria.values() or criteria['open_today'] == 'closed' else ('yes' if all(v in ('yes', 'open') for v in criteria.values()) else 'unknown')
        out.append({'branch_id': bid, 'name': db.execute('SELECT name FROM entities WHERE entity_id=?', (bid,)).fetchone()[0], 'verdict': verdict,
                    'criteria': criteria, 'answered_criteria': answered,
                    'evidence': {'rapid_cold_plunge': rapid['evidence'], 'foreigner': foreign['evidence'], 'access': access['evidence'], 'day_pass': passes, 'closures': closures}})
    return out
