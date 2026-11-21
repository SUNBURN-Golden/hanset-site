"""Bounded, local operator approvals; no DNS, fetches or inferred official trust."""
import hashlib
import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from hanset_active.util.urls import canonicalize, is_collect_denied, is_denied, is_excluded, literal_private, registrable_domain

from . import CollectError
from .config import canonical_instagram, handle_valid, outside
from .normalize import stable_json

MAX_POLICY_BYTES = 512_000
MAX_APPROVALS = 1000
_KEYS = {'venue_id', 'venue_key', 'source_url', 'source_kind', 'registrable_domain',
         'domain_boundary_reviewed', 'document_origin_group', 'approved_at', 'official_handle', 'review_only'}


def source_identity(url, kind, *, review_only=False):
    if not isinstance(url, str) or len(url) > 2000:
        raise ValueError
    if kind == 'instagram':
        if review_only:
            raise ValueError
        return canonical_instagram(url)[0]
    if kind != 'web':
        raise ValueError
    parsed = urlsplit(url)
    # Reject credentials and sensitive query content rather than sanitizing it
    # into a different approved source identity.
    canonical = canonicalize(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password or '[token]' in canonical or '[email]' in canonical or '[phone]' in canonical:
        raise ValueError
    for identity in (url, canonical):
        if literal_private(identity) or is_collect_denied(identity):
            raise ValueError
        if not review_only and (is_denied(identity) or is_excluded(identity)):
            raise ValueError
    return canonical


def _reviewed_domain(url, domain):
    host = urlsplit(url).hostname
    if not isinstance(domain, str) or not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+', domain):
        return False
    if not (host == domain or host.endswith('.' + domain)):
        return False
    # The helper is authoritative only for its small known suffix set and
    # common single-label suffixes. Other boundaries require the explicit
    # operator attestation already required by the policy schema.
    known = ('co.kr', 'or.kr', 'go.kr', 'ac.kr', 'co.uk', 'com.au', 'co.jp')
    if domain in known:
        return False
    if any(host.endswith('.' + suffix) for suffix in known) or host.rsplit('.', 1)[-1] in ('com', 'org', 'net', 'edu', 'gov', 'example', 'test', 'invalid'):
        return domain == registrable_domain(url)
    return len(domain.split('.')) >= 2


def load_policy(path=None, *, today=None):
    """Missing policy grants nothing. A supplied malformed file fails closed."""
    if path is None:
        return {'approvals': [], 'fingerprint': None}
    try:
        raw_path = Path(path)
        if raw_path.is_symlink() or not raw_path.is_file() or raw_path.stat().st_size > MAX_POLICY_BYTES:
            raise ValueError
        path = outside(raw_path)
        with path.open('rb') as stream:
            blob = stream.read(MAX_POLICY_BYTES + 1)
        if len(blob) > MAX_POLICY_BYTES:
            raise ValueError
        data = json.loads(blob)
        if not isinstance(data, dict) or set(data) != {'schema_version', 'approvals'} or type(data['schema_version']) is not int or data['schema_version'] != 1:
            raise ValueError
        rows = data['approvals']
        if not isinstance(rows, list) or len(rows) > MAX_APPROVALS:
            raise ValueError
        approvals = []
        for row in rows:
            required = _KEYS - {'official_handle', 'review_only'}
            if not isinstance(row, dict) or set(row) - _KEYS or not required <= set(row):
                raise ValueError
            if not all(isinstance(row[key], str) and 0 < len(row[key]) <= 200 for key in ('venue_id', 'venue_key', 'document_origin_group', 'approved_at')):
                raise ValueError
            approved = date.fromisoformat(row['approved_at'])
            if approved.isoformat() != row['approved_at'] or (today and approved > today):
                raise ValueError
            review_only = row.get('review_only', False)
            if type(review_only) is not bool or ('review_only' in row and row['source_kind'] != 'web'):
                raise ValueError
            url = source_identity(row['source_url'], row['source_kind'], review_only=review_only)
            if row['domain_boundary_reviewed'] is not True or not _reviewed_domain(url, row['registrable_domain']):
                raise ValueError
            if row['source_kind'] == 'instagram':
                if not handle_valid(row.get('official_handle')):
                    raise ValueError
                canonical_instagram(url, handle=row['official_handle'])
            elif 'official_handle' in row:
                raise ValueError
            approvals.append({**row, 'source_url': url})
        approvals.sort(key=stable_json)
        return {'approvals': approvals, 'fingerprint': hashlib.sha256(stable_json(approvals).encode()).hexdigest()}
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, CollectError):
        raise CollectError('promotion_policy_invalid') from None


def approval_for(claim, policy):
    try:
        kind = claim['source_kind']
        url = source_identity(claim['source_url'], kind, review_only=kind == 'web')
    except (ValueError, TypeError, CollectError):
        return None, 'invalid_source_url'
    matches = [row for row in policy['approvals'] if row['source_url'] == url]
    if len(matches) > 1:
        return None, 'ambiguous_source_approval'
    if not matches:
        return None, 'source_not_approved'
    row = matches[0]
    if any(row[key] != claim[key] for key in ('venue_id', 'venue_key', 'source_kind')):
        return None, 'venue_approval_mismatch'
    return row, None


def independence(left, right):
    """Approvals attest origin groups; hashes can veto, never prove independence."""
    a, b = left['approval'], right['approval']
    if a['source_kind'] == b['source_kind'] == 'instagram':
        return None
    if (a['source_url'] == b['source_url'] or a['document_origin_group'] == b['document_origin_group']
            or (left.get('final_url') and left['final_url'] == right.get('final_url'))):
        return None
    da, db = a['registrable_domain'], b['registrable_domain']
    if da == db or da.endswith('.' + db) or db.endswith('.' + da):
        return None
    if left.get('content_hash') and left['content_hash'] == right.get('content_hash'):
        return None
    return {'claim_ids': sorted([left['claim_id'], right['claim_id']]),
            'source_urls': sorted([a['source_url'], b['source_url']]),
            'source_approvals': [
                {'source_url': row['source_url'], 'authority': 'review_only' if row.get('review_only') else 'official'}
                for row in sorted((a, b), key=lambda row: row['source_url'])],
            'registrable_domains': sorted([da, db]),
            'document_origin_groups': sorted([a['document_origin_group'], b['document_origin_group']]),
            'web_source_required': True}
