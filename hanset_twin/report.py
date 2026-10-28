from .query import q_rapid_cold_foreigner_day_pass, q1_excluded

def summary(db, today):
    one = lambda sql: [tuple(r) for r in db.execute(sql)]
    q = q_rapid_cold_foreigner_day_pass(db, today)
    return {
        'entities_by_type': dict(one('SELECT entity_type,count(*) FROM entities GROUP BY 1')),
        'facts_by_status_open': dict(one('SELECT status,count(*) FROM facts WHERE effective_to IS NULL GROUP BY 1')),
        'facts_by_status_all': dict(one('SELECT status,count(*) FROM facts GROUP BY 1')),
        'closed_periods': dict(one("SELECT closed_reason,count(*) FROM facts WHERE effective_to IS NOT NULL GROUP BY 1")),
        'open_conflicts': dict(one("SELECT reason,count(*) FROM conflicts WHERE state='open' GROUP BY 1")),
        'review_queue': dict(one("SELECT kind,count(*) FROM review_queue WHERE state='open' GROUP BY 1")),
        'quality_issues': dict(one("SELECT check_id,count(*) FROM quality_issues WHERE state='open' GROUP BY 1")),
        'stage_failures': dict(one("SELECT state,count(*) FROM stage_failures GROUP BY 1")),
        'q1': {'branches': len(q), 'verdicts': {v: sum(x['verdict'] == v for x in q) for v in ('yes', 'no', 'unknown')},
               'answered_criteria_total': sum(x['answered_criteria'] for x in q), 'criteria_total': 5 * len(q),
               'by_criterion': {c: {s: sum(x['criteria'][c] == s for x in q) for s in sorted({x['criteria'][c] for x in q})} for c in (q[0]['criteria'] if q else {})}},
        'q1_excluded_branches': [x['name'] for x in q1_excluded(db)],
    }
