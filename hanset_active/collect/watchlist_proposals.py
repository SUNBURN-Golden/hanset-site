"""Propose profiles linked by guarded official pages; never request Instagram."""
import hashlib
import json
import re
from collections import defaultdict
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from hanset_active.util.urls import registrable_domain

from . import CollectError
from .config import canonical_instagram, domain_allowed, handle_valid
from .ground import redact
from .plan import MAX_PLAN_BYTES, _hash, _ident, _keys, _read, _text, _write

PROPOSAL_KEYS = ('venue_id venue_key handle official_page_url resolved_page_url '
                 'official_domain profile_url observed_at content_hash status reason')
PROFILE_RE = re.compile(r'https://(?:www\.)?instagram\.com/([A-Za-z0-9_][A-Za-z0-9._]{0,29})/?')
CLASSIFICATIONS = {('eligible', 'ok'), ('rejected', 'shared_handle'),
                   ('needs_review', 'ambiguous_handle_count'), ('needs_review', 'snapshot_overflow')}


def inspect_page(job, fetched, *, observed_at, disposition):
    """Inspect only pages that already passed every normal web guard.

    This function performs no I/O. Requested and final URLs must share one
    approved official domain. Invalid anchors are discarded, never guessed.
    """
    if disposition != 'ready' or fetched.outcome != 'ok' or not job.get('source_authorization'):
        return []
    requested, final = job['url'], fetched.url
    try:
        if (not domain_allowed(requested, job['domains']) or not domain_allowed(final, job['domains'])
                or registrable_domain(requested) != registrable_domain(final)
                or redact([requested, final, job['venue_id'], job['venue_key']])
                != [requested, final, job['venue_id'], job['venue_key']]):
            return []
        # Do not retain tracking/auth query strings as proposal evidence.
        if any(urlsplit(url).query or urlsplit(url).fragment or urlsplit(url).username for url in (requested, final)):
            return []
    except (TypeError, ValueError):
        return []
    soup = BeautifulSoup(fetched.content or b'', 'lxml')
    for node in soup.select('script,style,noscript,template'):
        node.decompose()
    entries = {}
    for anchor in soup.select('a[href]'):
        href = anchor.get('href', '')
        match = PROFILE_RE.fullmatch(href)
        if not match or not handle_valid(match[1]) or redact(href) != href:
            continue
        try:
            profile, kind = canonical_instagram(href)
        except CollectError:
            continue
        if kind != 'profile':
            continue
        handle = match[1].lower()
        entries[handle] = {
            'venue_id': job['venue_id'], 'venue_key': job['venue_key'], 'handle': handle,
            'official_page_url': requested, 'resolved_page_url': final,
            'official_domain': registrable_domain(final),
            'profile_url': profile.lower(), 'observed_at': observed_at,
            'content_hash': hashlib.sha256(fetched.content or b'').hexdigest(),
            'status': 'eligible', 'reason': 'ok',
        }
    return list(entries.values())


def classify(entries):
    """Only a one-venue/one-handle association is eligible in this snapshot."""
    accounts, venues = defaultdict(set), defaultdict(set)
    for entry in entries:
        key = entry['venue_id'], entry['venue_key']
        accounts[entry['handle']].add(key)
        venues[key].add(entry['handle'])
    result = []
    for entry in sorted(entries, key=lambda e: (e['venue_id'], e['venue_key'], e['handle'], e['official_page_url'])):
        key = entry['venue_id'], entry['venue_key']
        if len(accounts[entry['handle']]) > 1:
            status, reason = 'rejected', 'shared_handle'
        elif len(venues[key]) > 1:
            status, reason = 'needs_review', 'ambiguous_handle_count'
        else:
            status, reason = 'eligible', 'ok'
        result.append({**entry, 'status': status, 'reason': reason})
    return result


def _valid(entry, day):
    from datetime import datetime, timedelta
    try:
        if (not _keys(entry, PROPOSAL_KEYS) or not _ident(entry) or not handle_valid(entry['handle'])
                or not _hash(entry['content_hash']) or not _text(entry['official_domain'], 253)
                or (entry['status'], entry['reason']) not in CLASSIFICATIONS
                or not all(_text(entry[k], 500) for k in ('official_page_url', 'resolved_page_url', 'profile_url'))):
            return False
        stamp = datetime.fromisoformat(entry['observed_at'])
        profile, kind = canonical_instagram(entry['profile_url'], handle=entry['handle'])
        return (stamp.date().isoformat() == day and stamp.utcoffset() == timedelta(hours=9)
                and kind == 'profile' and profile == entry['profile_url']
                and all(domain_allowed(entry[k], {entry['official_domain']})
                        for k in ('official_page_url', 'resolved_page_url')))
    except (KeyError, TypeError, ValueError, CollectError):
        return False


def write_proposals(settings, entries, *, day):
    """Merge today's already-fetched evidence, reclassify globally, and persist.

    Prior days are not treated as a current official-link confirmation. This
    artifact never changes the live schema-1 watchlist or grants read permission.
    """
    path = settings.data_dir / 'watchlist-proposals.json'
    prior = []
    try:
        saved = _read(path)
        if (_keys(saved, 'schema_version day proposals auto_publish_to_site')
                and saved['schema_version'] == 1 and saved['day'] == day
                and saved['auto_publish_to_site'] is False
                and isinstance(saved['proposals'], list) and len(saved['proposals']) <= 1000
                and all(_valid(entry, day) for entry in saved['proposals'])):
            prior = saved['proposals']
    except (OSError, ValueError, TypeError):
        pass
    combined = {}
    for entry in prior + entries:
        if _valid(entry, day):
            key = entry['venue_id'], entry['venue_key'], entry['handle'], entry['official_page_url']
            combined[key] = entry
    # Never truncate then classify: shared associations outside the bound must
    # not turn a retained account into an eligible proposal.
    classified = classify(list(combined.values()))
    artifact = {'schema_version': 1, 'day': day, 'proposals': classified,
                'auto_publish_to_site': False}
    def size():
        return len((json.dumps(artifact, ensure_ascii=False, sort_keys=True) + '\n').encode())
    if (len(classified) > 1000 or size() > MAX_PLAN_BYTES
            or any(e['reason'] == 'snapshot_overflow' for e in prior)):
        # Losing an association must never make another handle eligible. Keep
        # a fixed-code overflow marker on retained rows for the entire day.
        artifact['proposals'] = [{**e, 'status': 'needs_review', 'reason': 'snapshot_overflow'}
                                 for e in classified[:1000]]
        while size() > MAX_PLAN_BYTES:
            artifact['proposals'] = artifact['proposals'][:len(artifact['proposals']) // 2]
    _write(path, artifact)
