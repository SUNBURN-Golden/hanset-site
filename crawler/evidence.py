"""Field-level evidence, scoped conflicts, explicit operator review."""
import datetime as dt
from .config import Rejected, policy_for
from .schema import STAGES, digest


def observations(cfg, url, venue_id, facts, now, reservation_id):
    try:
        p = policy_for(cfg, url, now=now)
    except Rejected:
        p = {}
    official = bool(venue_id and venue_id in p.get('official_venues', []))
    checked = dt.datetime.fromtimestamp(now, dt.timezone.utc).date().isoformat()
    result = []
    for n, f in enumerate(facts):
        result.append({**f, 'evidence_id': digest([reservation_id, n])[:24],
                       'source_url': url, 'source_type': 'official' if official else 'clue',
                       'checked_at': checked, 'verification_status': 'candidate',
                       'rate_kind': ('official_rate' if official else 'clue_rate') if f['field'] == 'rates' else None})
    return result


def overlapping(a, b):
    sa, sb = a.get('scope', {}), b.get('scope', {})
    keys = ('audience', 'day', 'service', 'unit', 'currency')
    if any(sa.get(k) != sb.get(k) for k in keys):
        return False
    return max(sa.get('valid_from', '0001-01-01'), sb.get('valid_from', '0001-01-01')) <= min(
        sa.get('valid_until', '9999-12-31'), sb.get('valid_until', '9999-12-31'))


def conflicts(records):
    found = []
    for i, a in enumerate(records):
        if not a.get('venue_id'):
            continue
        for b in records[i + 1:]:
            if a['venue_id'] != b.get('venue_id'):
                continue
            for fa in a['extracted_facts']:
                for fb in b['extracted_facts']:
                    if (fa['field'], fa.get('key')) == (fb['field'], fb.get('key')) and overlapping(fa, fb) and fa['value'] != fb['value']:
                        found.append({'conflict_id': digest(sorted([fa['evidence_id'], fb['evidence_id']])), 'records': [a['record_id'], b['record_id']], 'field': fa['field'],
                                      'evidence_ids': [fa['evidence_id'], fb['evidence_id']],
                                      'values': [fa['value'], fb['value']]})
    return found


def current_conflicts(db, records):
    decisions = {cid: {'evidence_id': eid, 'reason': reason, 'at': at}
                 for cid, eid, reason, at in db.execute('SELECT id,evidence_id,reason,at FROM conflict_resolutions')}
    by_record = {r['record_id']: [] for r in records}
    for issue in conflicts(records):
        decision = decisions.get(issue['conflict_id'])
        issue['resolved'] = bool(decision and decision['evidence_id'] in issue['evidence_ids'])
        if issue['resolved']:
            issue['decision'] = decision
        for rid in issue['records']:
            by_record[rid].append(issue)
    return by_record


def fact_status(fact, now):
    try:
        checked = dt.date.fromisoformat(fact.get('checked_at'))
    except (TypeError, ValueError):
        return '오래됨'
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    if not 0 <= (today - checked).days <= 90:
        return '오래됨'
    if fact.get('verification_status') != 'verified':
        return '확인 중'
    return 'verified'


def check_review(record, ids, now, cfg, *, conflicts, require_verified=False):
    """Return the eligible selection without changing the record or its facts."""
    if any(not c['resolved'] for c in conflicts) or not record.get('venue_id') or record.get('privacy_status') != 'passed':
        raise Rejected('review_required')
    facts = {f['evidence_id']: f for f in record['extracted_facts']}
    selected_ids = set(ids or ())
    if not selected_ids or selected_ids - facts.keys():
        raise Rejected('invalid_fact_selection')
    selected = [facts[eid] for eid in ids]
    for issue in conflicts:
        losing_ids = set(issue['evidence_ids']) - {issue['decision']['evidence_id']}
        if losing_ids & selected_ids:
            raise Rejected('conflict_decision_excludes_fact')
    for fact in selected:
        if fact.get('source_type') != 'official' or not all(fact.get(k) for k in ('locator', 'quote', 'source_url')):
            raise Rejected('not_official_evidence')
        approval = policy_for(cfg, fact['source_url'], now=now)
        if record['venue_id'] not in approval.get('official_venues', []):
            raise Rejected('official_approval_revoked')
        status = fact_status(fact, now)
        if status == '오래됨':
            raise Rejected('stale_evidence')
        if require_verified and status != 'verified':
            raise Rejected('review_required')
    return selected


def current_review_blocker(record, now, cfg, conflicts):
    if record['pipeline_stage'] in STAGES[3:]:
        try:
            check_review(record, record.get('selected_evidence_ids'), now, cfg,
                         conflicts=conflicts, require_verified=True)
        except Rejected as exc:
            return str(exc)
    return None


def retain_review(previous, record, now, cfg, *, previous_conflicts, conflicts):
    """Transfer review only for the same selected evidence, still fresh and approved.

    Observation IDs and check dates change on every recheck. Values, scope,
    location, quote, venue and source trust must all match the operator's review.
    """
    try:
        selected = check_review(previous, previous.get('selected_evidence_ids'), now, cfg,
                                conflicts=previous_conflicts, require_verified=True)
    except Rejected as exc:
        record['verification_status'] = 'review_required'
        return 'conflict_or_stale' if str(exc) == 'stale_evidence' else str(exc)
    keys = ('field', 'key', 'value', 'scope', 'locator', 'quote', 'source_url', 'source_type', 'rate_kind')
    ids = []
    for old in selected:
        match = next((f for f in record['extracted_facts'] if all(f.get(k) == old.get(k) for k in keys)), None)
        if previous['venue_id'] != record['venue_id'] or not match:
            record['verification_status'] = 'review_required'
            return 'evidence_changed'
        ids.append(match['evidence_id'])
    try:
        review(record, ids, now, cfg, conflicts=conflicts)
    except Rejected as exc:
        record['verification_status'] = 'review_required'
        return str(exc)
    record['pipeline_stage'] = previous['pipeline_stage']
    return 'unchanged_recheck'


def review(record, ids, now, cfg, *, conflicts):
    for fact in check_review(record, ids, now, cfg, conflicts=conflicts):
        fact['verification_status'] = 'verified'
    record['selected_evidence_ids'] = sorted(ids)
    record['verification_status'] = 'verified'
    return record
