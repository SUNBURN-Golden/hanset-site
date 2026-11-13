"""Approved official URL lists. Search leads are not fetched unless the domain was approved."""
import json
import re
from urllib.parse import urlsplit, unquote, parse_qsl

from hanset_active.util.urls import is_denied, is_excluded, literal_private, registrable_domain

from . import CollectError
from .config import MAX_TARGETS, domain_allowed, official_domains

_TAVILY_REASON = re.compile(r'tavily_[a-z_]{1,40}')


def canonical_requested_url(url, *, testing=False):
    """Reject unsafe identities before canonicalization; never repair redactions."""
    from .ground import secrets
    from hanset_active.util.pii import strip_pii
    from hanset_active.util.urls import canonicalize
    if not isinstance(url, str) or len(url) > 500:
        raise ValueError('unsafe_url')
    decoded = unquote(url)
    credential_keys = {'password', 'passwd', 'pwd', 'key', 'api_key', 'apikey', 'access_token',
                       'token', 'auth', 'authorization', 'cookie', 'session', 'sid', 'secret',
                       'signature', 'sig'}
    if (not _https_or_test(url, testing=testing) or strip_pii(decoded) != decoded
            or any(key.casefold() in credential_keys for key, _ in parse_qsl(urlsplit(decoded).query))
            or re.search(r'\[(?:token|phone|email|redacted[^\]]*)\]', decoded, re.I)
            or any(secret in decoded for secret in secrets())):
        raise ValueError('unsafe_url')
    return canonicalize(url)


def target_fingerprint(target):
    from .normalize import stable_json
    from .store import digest
    return digest(stable_json({
        'venue_id': target['venue_id'], 'venue_key': target['venue_key'],
        'official_urls': sorted(canonical_requested_url(url, testing=target.get('_testing', False))
                                for url in target['urls']),
        'official_domains': sorted(target['domains']),
    }))


def source_authorization(target, requested_url, final_url=None, *, testing=False):
    """Only an exact enabled official URL, never a domain-only search lead."""
    try:
        requested = canonical_requested_url(requested_url, testing=testing)
        final = canonical_requested_url(final_url or requested_url, testing=testing)
        urls = {canonical_requested_url(url, testing=testing) for url in target['urls']}
        if requested not in urls or not domain_allowed(final, target['domains']):
            return None
        return {'source_identity': requested, 'requested_url': requested, 'final_url': final,
                'target_fingerprint': target_fingerprint({**target, '_testing': testing})}
    except (ValueError, TypeError, KeyError):
        return None


def _https_or_test(url, *, testing):
    parsed = urlsplit(url)
    loopback = parsed.hostname in ('127.0.0.1', '::1', 'localhost')
    if parsed.scheme == 'https':
        allowed_scheme = True
    elif testing and parsed.scheme == 'http' and loopback:
        allowed_scheme = True
    else:
        allowed_scheme = False
    if not allowed_scheme or parsed.username or parsed.password:
        return False
    if literal_private(url) and not (testing and loopback):
        return False
    return not is_denied(url) and not is_excluded(url)


def parse_targets(path, *, testing, disabled=None):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 512_000:
            raise ValueError
        data = json.loads(path.read_bytes().decode())
        if data.get('schema_version') != 1 or not isinstance(data.get('targets'), list) or len(data['targets']) > MAX_TARGETS:
            raise ValueError
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
        raise CollectError('targets_invalid') from None
    targets = []
    for entry in data['targets']:
        if not isinstance(entry, dict) or type(entry.get('enabled', True)) is not bool:
            raise CollectError('targets_invalid')
        if not entry.get('enabled', True):
            # Disabled rows have always been skipped before strict validation.
            if disabled is not None and all(isinstance(entry.get(k), str) and
                                            0 < len(entry[k]) <= 200
                                            for k in ('venue_id', 'venue_key')):
                disabled.append({k: entry[k] for k in ('venue_id', 'venue_key')})
            continue
        try:
            if not all(isinstance(entry.get(key), str) and entry[key] and len(entry[key]) <= 200 for key in ('venue_id', 'venue_key', 'name')):
                raise ValueError
            if is_excluded(entry['name'], entry['venue_key']):
                raise ValueError
            if 'official_domains' in entry and not isinstance(entry['official_domains'], list):
                raise ValueError
            urls = entry.get('official_urls', [])
            if not isinstance(urls, list) or len(urls) > 3 or not all(isinstance(url, str) and len(url) <= 500 for url in urls):
                raise ValueError
            if any(not _https_or_test(url, testing=testing) for url in urls):
                raise ValueError
            for url in urls:
                canonical_requested_url(url, testing=testing)
            domains = official_domains(entry)
            if not domains:
                domains = {registrable_domain(url) for url in urls}
            if any(not domain_allowed(url, domains) for url in urls):
                raise ValueError
            spec = entry.get('tavily')
            if spec is not None:
                if not isinstance(spec, dict) or set(spec) != {'venue_id', 'field'}:
                    raise ValueError
                if not all(isinstance(spec[key], str) and spec[key] and len(spec[key]) <= 200 for key in spec):
                    raise ValueError
            if not urls and not spec:
                raise ValueError
        except (TypeError, ValueError, KeyError):
            raise CollectError('targets_invalid') from None
        targets.append({
            'venue_id': entry['venue_id'],
            'venue_key': entry['venue_key'],
            'name': entry['name'],
            'urls': urls,
            'domains': domains,
            'tavily': spec,
        })
    return targets


def _recorded_domains(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return set()
    if not isinstance(value, list):
        return set()
    # These are domain records, not guessed URLs or paths. Reject malformed
    # records before constructing a root URL; canonical URL checks run later.
    return {registrable_domain(d) for d in value if isinstance(d, str)
            and re.fullmatch(r'[A-Za-z0-9.-]{1,253}', d)}


def ownership_index(venues, aliases, seed_venues, seed_domains):
    """Materialized ownership only; a frozen-seed marker is never a join key."""
    from collections import defaultdict
    from hanset_active.venues import normalize_name
    names, domains, resolution_domains, by_id = defaultdict(set), {}, {}, {}
    for venue in venues:
        vid = venue['venue_id']
        by_id[vid] = venue
        for name in (venue.get('name'), venue.get('name_ko')):
            if name and normalize_name(name):
                names[normalize_name(name)].add(vid)
        domains[vid] = _recorded_domains(venue.get('domains'))
        resolution_domains[vid] = set(domains[vid])
        if venue.get('website'):
            try:
                resolution_domains[vid].add(registrable_domain(venue['website']))
            except (ValueError, TypeError):
                pass
    for alias in aliases:
        if alias['venue_id'] in by_id and alias['normalized_name']:
            names[alias['normalized_name']].add(alias['venue_id'])
    allowed_seeds = {r['domain'] for r in seed_domains}
    for seed in seed_venues:
        matches = names.get(seed['normalized_name'], set())
        if seed['domain'] in allowed_seeds and len(matches) == 1:
            domains[next(iter(matches))].update(_recorded_domains([seed['domain']]))
    # Manual resolution uses the venue's website/domains, never inferred seeds.
    return by_id, names, domains, resolution_domains


def resolve_identity(target, ownership):
    """Exact identity first, otherwise the union of exact names and domains."""
    from hanset_active.venues import normalize_name
    by_id, names, _, domains = ownership
    exact = by_id.get(target['venue_id'])
    if exact and exact['venue_key'] == target['venue_key']:
        return target, None
    matches = set()
    for name in (target.get('name'), target['venue_key']):
        if name:
            matches.update(names.get(normalize_name(name), ()))
    requested = {registrable_domain(d) for d in target.get('domains', ())}
    requested.update(registrable_domain(u) for u in target.get('urls', ()))
    for vid, owned in domains.items():
        if requested & owned:
            matches.add(vid)
    if len(matches) == 1:
        venue = by_id[next(iter(matches))]
        return {**target, 'venue_id': venue['venue_id'], 'venue_key': venue['venue_key']}, None
    return target, 'manual_identity_ambiguous' if matches else 'manual_identity_unmatched'


def derive_target(venue, domains, notes):
    """No discovery or HTTP upgrade; roots require independently owned domains."""
    from hanset_active.util.followups import portal_domains
    portals = portal_domains()
    urls, approved = [], set()
    website = venue.get('website') or ''
    candidates = ([website] if website else []) + ['https://' + d + '/' for d in sorted(domains)]
    if website.lower().startswith('http://'):
        notes['derived_http_only'] += 1
    for url in candidates:
        if url.lower().startswith('http://'):
            continue
        try:
            canonical = canonical_requested_url(url)
            domain = registrable_domain(canonical)
            permitted = official_domains({'official_domains': [domain]})
            if domain in portals or not domain_allowed(canonical, permitted):
                raise ValueError
        except (CollectError, ValueError, TypeError, UnicodeError):
            notes['derived_domain_denied'] += 1
            continue
        if canonical not in urls and len(urls) < 3:
            urls.append(canonical)
            approved.add(domain)
    if not urls:
        return None
    return {'venue_id': venue['venue_id'], 'venue_key': venue['venue_key'],
            'name': venue.get('name') or venue['venue_key'], 'urls': urls,
            'domains': approved, 'tavily': None}


def merge_targets(manual, derived, disabled, ownership, notes, *, testing=False):
    """Manual authorization wins; capacity is filled in the supplied local order."""
    def ident(t):
        return t['venue_id'], t['venue_key']

    effective = {}
    for target in manual:
        target, reason = resolve_identity(target, ownership)
        if reason:
            notes[reason] += 1
        key = ident(target)
        if key not in effective:
            effective[key] = {**target, 'urls': [], 'domains': set(target['domains'])}
        base = effective[key]
        # Multiple manual rows may explicitly approve different domains. Only
        # manual input may extend this set, never a derived candidate.
        base['domains'].update(target['domains'])
        if base['tavily'] is None:
            base['tavily'] = target['tavily']
        for url in target['urls']:
            canonical = canonical_requested_url(url, testing=testing)
            if (canonical not in {canonical_requested_url(u, testing=testing) for u in base['urls']}
                    and domain_allowed(url, base['domains']) and len(base['urls']) < 3):
                base['urls'].append(url)
    suppressed = {ident(resolve_identity(t, ownership)[0]) for t in disabled}
    for target in derived:
        key = ident(target)
        if key in effective:
            base = effective[key]
            for url in target['urls']:
                if not domain_allowed(url, base['domains']):
                    notes['derived_domain_denied'] += 1
                elif (canonical_requested_url(url) not in
                      {canonical_requested_url(u, testing=testing) for u in base['urls']}
                      and len(base['urls']) < 3):
                    base['urls'].append(url)
        elif key in suppressed:
            notes['derived_suppressed'] += 1
        elif len(effective) >= MAX_TARGETS:
            notes['derived_truncated'] += 1
        else:
            effective[key] = target
    return list(effective.values())


def lead_allowed(url, domains, *, testing):
    return _https_or_test(url, testing=testing) and domain_allowed(url, domains)


def resolve_jobs(targets, *, search, db, clock, testing):
    """Return fetch jobs and public skip notes. Unapproved lead URLs are not returned."""
    jobs, notes = [], []
    for target in targets:
        urls = list(target['urls'])
        if not urls and target['tavily']:
            if not target['domains']:
                notes.append({'url': '', 'disposition': 'tavily_unbounded'})
                continue
            if search is None or db is None:
                notes.append({'url': '', 'disposition': 'tavily_skipped'})
                continue
            try:
                outcome = search(db, target['tavily']['venue_id'], target['tavily']['field'], target='official_page', clock=clock)
            except Exception:
                notes.append({'url': '', 'disposition': 'tavily_skipped'})
                continue
            reason = getattr(outcome, 'reason', 'tavily_skipped')
            if getattr(outcome, 'status', '') != 'ok':
                notes.append({'url': '', 'disposition': reason if _TAVILY_REASON.fullmatch(reason or '') else 'tavily_skipped'})
                continue
            chosen = None
            for lead in getattr(outcome, 'leads', ()) or ():
                url = getattr(lead, 'url', '')
                if chosen is None and lead_allowed(url, target['domains'], testing=testing):
                    chosen = url
            if not chosen:
                notes.append({'url': '', 'disposition': 'tavily_skipped'})
                continue
            urls.append(chosen)
        for url in urls[:3]:
            jobs.append({
                'venue_id': target['venue_id'],
                'venue_key': target['venue_key'],
                'name': target['name'],
                'url': url,
                'domains': target['domains'],
                'source_authorization': source_authorization(target, url, testing=testing),
            })
    return jobs, notes
