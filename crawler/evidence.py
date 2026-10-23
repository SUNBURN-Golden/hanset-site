"""Field-level evidence, scoped conflicts, explicit operator review."""
import datetime as dt
import json
from .config import Rejected, policy_for
from .schema import digest


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


def fact_status(fact, now):
    checked = dt.date.fromisoformat(fact['checked_at'])
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    if not 0 <= (today - checked).days <= 90:
        return '오래됨'
    if fact.get('verification_status') != 'verified':
        return '확인 중'
    return 'verified'


def retain_review(previous, record, now, cfg):
    """Transfer review only for the same selected evidence, still fresh and approved.

    Observation IDs and check dates change on every recheck. Values, scope,
    location, quote, venue and source trust must all match the operator's review.
    """
    selected = [f for f in previous['extracted_facts'] if f['evidence_id'] in previous['selected_evidence_ids']]
    if not selected or any(fact_status(f, now) != 'verified' for f in selected):
        record['verification_status'] = 'review_required'
        return 'conflict_or_stale'
    keys = ('field', 'key', 'value', 'scope', 'locator', 'quote', 'source_url', 'source_type', 'rate_kind')
    ids = []
    for old in selected:
        match = next((f for f in record['extracted_facts'] if all(f.get(k) == old.get(k) for k in keys)), None)
        if previous['venue_id'] != record['venue_id'] or not match:
            record['verification_status'] = 'review_required'
            return 'evidence_changed'
        ids.append(match['evidence_id'])
    try:
        review(record, ids, now, cfg)
    except Rejected:
        record['selected_evidence_ids'] = []
        record['verification_status'] = 'review_required'
        return 'approval_or_conflict_changed'
    record['pipeline_stage'] = previous['pipeline_stage']
    return 'unchanged_recheck'


def review(record, ids, now, cfg):
    if record.get('has_conflict') or not record.get('venue_id') or record.get('privacy_status') != 'passed':
        raise Rejected('review_required')
    facts = {f['evidence_id']: f for f in record['extracted_facts']}
    if not ids or set(ids) - facts.keys():
        raise Rejected('invalid_fact_selection')
    for eid in ids:
        f = facts[eid]
        if f['source_type'] != 'official' or not f.get('locator') or not f.get('quote'):
            raise Rejected('not_official_evidence')
        approval = policy_for(cfg, f['source_url'], now=now)
        if record['venue_id'] not in approval.get('official_venues', []):
            raise Rejected('official_approval_revoked')
        check = {**f, 'verification_status': 'verified'}
        if fact_status(check, now) != 'verified':
            raise Rejected('stale_evidence')
    for eid in ids:
        facts[eid]['verification_status'] = 'verified'
    record['selected_evidence_ids'] = sorted(ids)
    record['verification_status'] = 'verified'
    return record
