"""Deterministic internal assessment. Agreement grants no publication authority."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from itertools import combinations

from hanset_active.db import deterministic_id

from .normalize import VERSION, normalize_value, stable_json
from .promotion_policy import approval_for, independence

_PROVENANCE = {'web': ('web_verbatim', 'page_span', 1),
               'instagram': ('operator_summary', 'summary_span', 0)}


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.utcoffset() is not None else None
    except (TypeError, ValueError):
        return None


def _identity(claim):
    return all(isinstance(claim.get(key), str) and claim[key].strip() and
               claim[key].casefold() not in ('unknown', 'unresolved', '확인 중')
               for key in ('venue_id', 'venue_key'))


def _eligibility(claim, policy):
    reasons = []
    if claim.get('status') != 'source_claim' or claim.get('rights') != 'internal_review_only':
        reasons.append('claim_not_internal_source')
    if not _identity(claim):
        reasons.append('unresolved_venue')
    provenance = tuple(claim.get(key) for key in ('evidence_origin', 'evidence_role', 'verbatim_source'))
    if any(item is None for item in provenance):
        reasons.append('legacy_provenance_unrecorded')
    elif provenance != _PROVENANCE.get(claim.get('source_kind')):
        reasons.append('provenance_invalid')
    approval, reason = approval_for(claim, policy)
    if reason:
        reasons.append(reason)
    if claim['normalization']['reason']:
        reasons.append(claim['normalization']['reason'])
    return approval, reasons


def _group_key(item):
    return (item['venue_id'], item['venue_key'], item['field'], stable_json(item['normalization']['qualifiers']))


def _assessment_evidence(value):
    """Time-dependent display labels never advance persistence timestamps."""
    if isinstance(value, dict):
        return {key: _assessment_evidence(item) for key, item in value.items() if key != 'freshness'}
    if isinstance(value, list):
        return [_assessment_evidence(item) for item in value]
    return value


def assess(claims, existing, policy, *, now):
    """Reassess stored candidates even when approvals are absent or revoked.

    All normalizable claims participate in disagreements, including legacy and
    same-page observations. Invalid values are diagnostic evidence, never votes.
    """
    evaluated, groups = [], defaultdict(list)
    reasons = Counter()
    for raw in sorted(claims, key=lambda item: item['claim_id']):
        item = dict(raw)
        item['normalization'] = normalize_value(item['field'], item['value'])
        approval, errors = _eligibility(item, policy)
        item['promotion_eligible'] = not errors
        item['eligibility_reasons'] = errors
        item['approval'] = approval
        seen = timestamp(item.get('seen_at'))
        item['date_basis'] = 'seen_at'
        item['freshness'] = ('오래됨' if now - seen > timedelta(days=90) else 'fresh') if seen else 'unranked'
        reasons.update(errors)
        evaluated.append(item)
        if not item['normalization']['reason']:
            groups[_group_key(item)].append(item)

    conflicts, conflicting_keys = [], set()
    proposed = {}
    for group_key, items in sorted(groups.items()):
        by_value = defaultdict(list)
        for item in items:
            by_value[item['normalization']['comparison_key']].append(item)
        is_conflict = len(by_value) > 1
        conflict_id = deterministic_id('collector-conflict', VERSION, group_key)
        if is_conflict:
            conflicting_keys.add(group_key)
            dates = [parsed for item in items if (parsed := timestamp(item.get('seen_at'))) is not None]
            newest = max(dates) if dates else None
            observations = []
            for item in items:
                seen = timestamp(item.get('seen_at'))
                observations.append({key: value for key, value in item.items() if key != 'approval'} |
                                    {'is_newest': seen is not None and seen == newest})
            conflicts.append({'conflict_id': conflict_id, 'venue_id': group_key[0],
                              'venue_key': group_key[1], 'field': group_key[2],
                              'qualifiers': items[0]['normalization']['qualifiers'],
                              'scopes': [items[0]['normalization']['qualifiers'].get('scope', 'unspecified')],
                              'state': '확인 필요', 'date_basis': 'seen_at',
                              'newest_meaning': 'most recently observed',
                              'claim_ids': [item['claim_id'] for item in items],
                              'values': [item['value'] for item in items],
                              'observations': observations,
                              'newest_claim_ids': [item['claim_id'] for item in observations if item['is_newest']]})
        for value_key, supporting in sorted(by_value.items()):
            eligible = [item for item in supporting if item['promotion_eligible']]
            pairs = [rationale for left, right in combinations(eligible, 2)
                     if (rationale := independence(left, right)) is not None]
            if not pairs:
                continue
            first = supporting[0]
            candidate_id = deterministic_id('collector-fact-candidate', VERSION, group_key, value_key)
            proposed[candidate_id] = {
                'candidate_id': candidate_id, 'venue_id': first['venue_id'],
                'venue_key': first['venue_key'], 'field': first['field'],
                'qualifiers': first['normalization']['qualifiers'],
                'normalized_value': first['normalization']['normalized_value'],
                'normalizer_version': VERSION, 'status': 'fact_candidate',
                'rights': 'internal_review_only',
                'review_state': 'needs_confirmation' if is_conflict else 'awaiting_review',
                'conflict_id': conflict_id if is_conflict else None,
                'supporting_claim_ids': sorted(item['claim_id'] for item in supporting),
                'independence_rationale': pairs,
            }

    for stored in existing:
        candidate_id = stored['candidate_id']
        if candidate_id in proposed:
            item = proposed[candidate_id]
            item['supporting_claim_ids'] = sorted(set(item['supporting_claim_ids']) | set(stored['supporting_claim_ids']))
            item['created_at'], item['changed_at'] = stored['created_at'], stored['changed_at']
        else:
            item = {**stored, 'review_state': 'evidence_hold', 'independence_rationale': []}
            group_key = (item['venue_id'], item['venue_key'], item['field'], stable_json(item['qualifiers']))
            item['conflict_id'] = deterministic_id('collector-conflict', VERSION, group_key) if group_key in conflicting_keys else None
            if item['normalizer_version'] == VERSION:
                matching = [claim['claim_id'] for claim in groups.get(group_key, [])
                            if claim['normalization']['comparison_key'] == stable_json(item['normalized_value'])]
                item['supporting_claim_ids'] = sorted(set(item['supporting_claim_ids']) | set(matching))
            proposed[candidate_id] = item

    by_id = {item['claim_id']: item for item in evaluated}
    for item in proposed.values():
        item['conflict_state'] = '확인 필요' if item.get('conflict_id') else None
        item['eligible_supporting_claim_ids'] = [cid for cid in item['supporting_claim_ids']
                                               if cid in by_id and by_id[cid]['promotion_eligible']]
        item['assessment_digest'] = deterministic_id(
            'collector-assessment', policy['fingerprint'], item['review_state'],
            item['supporting_claim_ids'], item['independence_rationale'],
            _assessment_evidence([entry for entry in conflicts if entry['conflict_id'] == item.get('conflict_id')]),
            _assessment_evidence([by_id[cid] for cid in item['supporting_claim_ids'] if cid in by_id]))
    for item in evaluated:
        item.pop('approval', None)
    candidates = sorted(proposed.values(), key=lambda item: item['candidate_id'])
    return {'claims': evaluated, 'conflicts': conflicts, 'fact_candidates': candidates,
            'eligibility_reason_counts': dict(sorted(reasons.items())),
            'policy_fingerprint': policy['fingerprint'], 'normalizer_version': VERSION,
            'assessment_fingerprint': deterministic_id('collector-report', policy['fingerprint'],
                                                      _assessment_evidence(evaluated),
                                                      [item['assessment_digest'] for item in candidates]),
            'date_basis': 'seen_at', 'rights': 'internal_review_only', 'auto_publish_to_site': False}
