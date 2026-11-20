"""Optional daemon search hook: existing venue + open gap -> unverified leads."""
from dataclasses import replace
import re
import os

from .tavily import MAX_QUERY, SearchOutcome, _search
from ..config import load_profile
from ..util.pii import strip_pii
from ..util.urls import is_collect_denied, is_denied, is_excluded, literal_private

_TARGET_TERMS = {'official_page': '공식 홈페이지', 'instagram_handle': '공식 인스타그램 site:instagram.com'}


def discover_known_venue(db, venue_id, gap_field, *, target='official_page', clock=None, transport=None, public_source=False, dispatch_guard=None, on_dispatch=None, host_dir=None):
    """Read-only against the active DB. Never creates a venue, task, fact or grant.

    `transport` is an offline test seam with requests.Session.post's signature.
    Production callers omit it. All skips/errors are fixed codes; continue other
    daemon work and leave the gap unresolved. No credential arguments accepted.
    """
    # Resolve identity first; invalid arbitrary input is never echoed in outcomes.
    if not isinstance(venue_id, str) or not isinstance(gap_field, str):
        return SearchOutcome('skipped', 'tavily_invalid_venue')
    venue = db.execute('SELECT * FROM venues WHERE venue_id=?', (venue_id,)).fetchone()
    if not venue:
        return SearchOutcome('skipped', 'tavily_unknown_venue')
    fields = {entry['id'] for entry in load_profile()['fields']}
    if gap_field not in fields:
        return SearchOutcome('skipped', 'tavily_unsupported_gap')
    gap = db.execute('SELECT * FROM gaps WHERE venue_id=? AND field=?', (venue_id, gap_field)).fetchone()
    if not gap:
        return SearchOutcome('skipped', 'tavily_gap_missing')
    if gap['state'] != 'open':
        return SearchOutcome('skipped', 'tavily_gap_resolved')
    if not isinstance(target, str) or target not in _TARGET_TERMS:
        return SearchOutcome('skipped', 'tavily_unsupported_target')
    website = venue['website'] or ''
    if ((not public_source and (is_excluded(venue['name'] or '', website) or (website and is_denied(website))))
            or (public_source and website and (is_collect_denied(website) or literal_private(website)))):
        return SearchOutcome('skipped', 'tavily_venue_excluded')
    name = venue['name'] or ''
    city = venue['city'] or ''
    # Keep the stored branch-specific name; no AI identity or query operators.
    identity = ' '.join(re.sub(r'[^\w가-힣 .-]', ' ', name + ' ' + city).split())
    if not name.strip() or len(identity) > MAX_QUERY - 50 or strip_pii(identity) != identity:
        return SearchOutcome('skipped', 'tavily_invalid_identity')
    query = identity + ' ' + _TARGET_TERMS[target]
    outcome = _search(query, target, clock=clock, transport=transport, public_source=public_source,
                      dispatch_guard=dispatch_guard, on_dispatch=on_dispatch, host_dir=host_dir)
    # Normal IDs are deterministic hashes, but still minimize externally supplied IDs.
    secret = os.environ.get('TAVILY_API_KEY', '').strip()
    safe_id = venue_id.replace(secret, '[redacted]') if secret else venue_id
    safe_id = safe_id if re.fullmatch(r'[0-9a-f]{64}', safe_id) else strip_pii(safe_id)
    safe_id = (safe_id.replace(secret, '[redacted]') if secret else safe_id)[:128]
    return replace(outcome, venue_id=safe_id, gap_field=gap_field)
