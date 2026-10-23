"""Exclusive current stages and non-additive distinct-place counts."""
import json
import time
from .schema import STAGES
from .evidence import fact_status
from .rate_limiter import KST
import datetime as dt


def report(store, cfg, now=None):
    now = time.time() if now is None else now
    rows = [json.loads(r[0]) for r in store.db.execute('SELECT data FROM records')]
    indexes = [r for r in rows if r['item_type'] == 'discovery_index']
    rows = [r for r in rows if r['item_type'] != 'discovery_index']
    counts = {s: 0 for s in STAGES}
    venues = {s: set() for s in STAGES}
    all_venues = set()
    for r in rows:
        stage = r['pipeline_stage']
        selected = [f for f in r['extracted_facts'] if f['evidence_id'] in r['selected_evidence_ids']]
        if stage in STAGES[3:] and any(fact_status(f, now) != 'verified' for f in selected):
            stage = STAGES[1]
        counts[stage] += 1
        if r['venue_id']:
            venues[stage].add(r['venue_id'])
            all_venues.add(r['venue_id'])
    day = dt.datetime.fromtimestamp(now, KST).date().isoformat()
    counters = store.db.execute('SELECT * FROM counters WHERE day=? AND brand=?', (day, 'HANSET')).fetchone()
    used = {k: counters[k] if counters else 0 for k in ('candidates', 'new_bodies', 'attempts')}
    lifecycle = {k: v for k, v in store.db.execute('SELECT state,count(*) FROM reservations GROUP BY state')}
    accepted = store.db.execute('SELECT count(*) FROM reservations WHERE ack IS NOT NULL').fetchone()[0]
    milestones = {stage: store.db.execute("SELECT count(DISTINCT h.record_id) FROM history h JOIN records r ON r.id=h.record_id WHERE h.stage=? AND json_extract(r.data,'$.item_type')!='discovery_index'", (stage,)).fetchone()[0] for stage in STAGES}
    return {'auto_publish_to_site': False, 'stage_counts': counts,
            'unique_venues_total': len(all_venues),
            'unique_venue_counts_by_stage': {s: len(v) for s, v in venues.items()},
            'stage_venue_counts_are_additive': False, 'milestone_counts': milestones,
            'discovery_indexes': len(indexes),
            'access_holds': sum(bool(r.get('access_hold')) for r in rows + indexes),
            'quota_usage': {'budget_day': day, **used, 'remaining_candidates': cfg['daily_candidates'] - used['candidates'],
                            'remaining_new_bodies': cfg['daily_new_bodies'] - used['new_bodies'],
                            'remaining_attempts': cfg['daily_attempts'] - used['attempts'],
                            'reservation_states': lifecycle, 'accepted': accepted, 'rejected': store.db.execute('SELECT count(*) FROM audit').fetchone()[0],
                            'reserved_total': store.db.execute('SELECT count(*) FROM reservations').fetchone()[0],
                            'started_total': store.db.execute("SELECT count(*) FROM history WHERE reason='extraction_started'").fetchone()[0],
                            'expired': store.db.execute("SELECT count(*) FROM reservations WHERE expires<=? AND ack IS NULL", (now,)).fetchone()[0]},
            'blocked_domains': store.db.execute('SELECT count(*) FROM domains WHERE stopped_until>?', (now,)).fetchone()[0],
            'held_domains': store.db.execute('SELECT count(*) FROM domains WHERE owner IS NOT NULL').fetchone()[0],
            'privacy_rejections': store.db.execute("SELECT count(*) FROM audit WHERE reason IN ('private_context','privacy_not_passed','sensitive_url')").fetchone()[0],
            'lifecycle_counts_scope': 'all_time; daily_budget_counters_use_budget_day',
            'revoking': lifecycle.get('revoking', 0),
            'conflicts': sum(r['has_conflict'] for r in rows),
            'stale': sum(any(fact_status(f, now) == '오래됨' for f in r['extracted_facts']) for r in rows),
            'identity_unconfirmed': sum(r['venue_id'] is None for r in rows)}
