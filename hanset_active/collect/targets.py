"""Collector URL scope, official authorization, and frozen seed targets."""
import json
import re
from urllib.parse import urlsplit, unquote, parse_qsl
from uuid import NAMESPACE_URL, uuid5

from hanset_active.config import load_profile
from hanset_active.util.urls import is_collect_denied, is_excluded, literal_private, registrable_domain
from hanset_active.venues import is_article_title, normalize_name

from . import CollectError
from .config import MAX_TARGETS, domain_allowed, official_domains, official_site, scope_allowed

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
                                for url in target.get('official_urls', target['urls'])),
        'official_domains': sorted(target['domains']),
    }))


def source_authorization(target, requested_url, final_url=None, *, testing=False):
    """Only an exact enabled official URL, never a domain-only search lead."""
    try:
        requested = canonical_requested_url(requested_url, testing=testing)
        final = canonical_requested_url(final_url or requested_url, testing=testing)
        urls = {canonical_requested_url(url, testing=testing)
                for url in target.get('official_urls', target['urls'])}
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
    if not allowed_scheme or parsed.username is not None or parsed.password is not None:
        return False
    if literal_private(url) and not (testing and loopback):
        return False
    return not is_collect_denied(url)


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
            if not isinstance(urls, list) or not all(isinstance(url, str) and len(url) <= 500 for url in urls):
                raise ValueError
            unique = {}
            for url in urls:
                unique.setdefault(canonical_requested_url(url, testing=testing), url)
            urls = list(unique.values())
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
            'official_urls': list(urls),
            'domains': domains,
            'scope_domains': set(domains),
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


def ownership_index(venues, aliases):
    """Index recorded venue identity and domain ownership."""
    from collections import defaultdict
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
    return by_id, names, domains, resolution_domains


def resolve_identity(target, ownership):
    """Exact identity first, otherwise the union of exact names and domains."""
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


def _canonical_public_url(url, *, testing=False):
    canonical = canonical_requested_url(url, testing=testing)
    host = urlsplit(canonical).hostname
    if re.search(r'[^a-z0-9.:-]', host):
        raise ValueError('unsafe_url')
    return canonical


def fetch_url_allowed(url, scope_domains, *, testing=False):
    """Validate the URL and check the fetch scope before contacting a destination."""
    try:
        return scope_allowed(_canonical_public_url(url, testing=testing), scope_domains)
    except (ValueError, TypeError, UnicodeError):
        return False


def _add_seed_urls(target, seeds, *, manual=False, testing=False):
    known = {canonical_requested_url(url, testing=testing) for url in target['urls']}
    for seed in seeds:
        official = set(seed['official_urls'])
        for url in seed['urls']:
            if url in known or (manual and url in official):
                continue
            known.add(url)
            domain = registrable_domain(url)
            target['urls'].append(url)
            target['scope_domains'].add(domain)
            target.setdefault('seed_urls', set()).add(url)
            if url in official and domain in target['domains']:
                target['official_urls'].append(url)


def _derive_urls(website, domains, notes, *, seed_name=None):
    profile = load_profile()
    urls, official, approved = [], [], set()
    excluded_seed = seed_name is not None and is_excluded(seed_name, terms=profile['exclusion_terms'])
    candidates = ([website] if website else []) + ['https://' + d + '/' for d in sorted(domains)]
    if website.lower().startswith('http://'):
        notes['derived_http_only'] += 1
    for url in candidates:
        if url.lower().startswith('http://'):
            continue
        try:
            canonical = _canonical_public_url(url)
            first_party = not excluded_seed and official_site(canonical, profile=profile)
            if not first_party and seed_name is None:
                raise ValueError('unapproved_domain')
        except (ValueError, TypeError, UnicodeError):
            notes['derived_domain_denied'] += 1
            continue
        if canonical not in urls:
            urls.append(canonical)
            if first_party:
                official.append(canonical)
                approved.add(registrable_domain(canonical))
    return urls, official, approved


def derive_target(venue, domains, notes, *, seeds=()):
    """No discovery or HTTP upgrade; roots require independently owned domains."""
    urls, official, approved = _derive_urls(venue.get('website') or '', domains, notes)
    approved.update(domain for seed in seeds for domain in seed['domains'])
    target = {'venue_id': venue['venue_id'], 'venue_key': venue['venue_key'],
              'name': venue.get('name') or venue['venue_key'], 'urls': urls,
              'official_urls': official, 'domains': approved, 'scope_domains': set(approved),
              'tavily': None}
    _add_seed_urls(target, seeds)
    return target if urls else None


def derive_seed_targets(seeds, seed_domains, ownership, notes):
    from collections import defaultdict

    by_id, names, _, _ = ownership
    frozen_domains = {row['domain'] for row in seed_domains}
    grouped = defaultdict(list)
    for seed in seeds:
        name, website, domain = seed['name'] or '', seed['website'] or '', seed['domain'] or ''
        norm = normalize_name(name)
        if not norm or is_article_title(name):
            continue
        matches = () if is_excluded(name, website, domain) else names.get(norm, ())
        if len(matches) > 1:
            continue
        venue = by_id[next(iter(matches))] if matches else None
        if venue and (venue['state'] == 'closed' or is_article_title(venue['name'])
                      or is_excluded(venue['name'], venue['venue_key'])):
            continue
        # A website copied into seed_domains cannot turn HTTP into HTTPS.
        roots = {domain} if (not website and domain in frozen_domains
                             and _recorded_domains([domain])) else set()
        urls, official, approved = _derive_urls(website, roots, notes, seed_name=name)
        if not urls:
            continue
        if venue:
            venue_id, venue_key, name = venue['venue_id'], venue['venue_key'], venue['name']
        else:
            domain = registrable_domain(urls[0])
            seed_id = uuid5(NAMESPACE_URL, f'hanset-collector-seed:{norm}:{domain}')
            venue_id = venue_key = f'seed:{seed_id}'
        target = {'venue_id': venue_id, 'venue_key': venue_key, 'name': name,
                  'urls': urls, 'official_urls': official, 'domains': approved,
                  'scope_domains': {registrable_domain(url) for url in urls},
                  'tavily': None, 'seed_urls': set(urls)}
        grouped[venue_id, venue_key].append(target)
    targets = []
    for first, *remaining in grouped.values():
        first['domains'].update(domain for seed in remaining for domain in seed['domains'])
        _add_seed_urls(first, remaining)
        targets.append(first)
    return targets


def merge_targets(manual, derived, disabled, ownership, notes, *, seeds=(), testing=False):
    """Manual authorization wins; capacity is filled in the supplied local order."""
    def ident(t):
        return t['venue_id'], t['venue_key']

    effective, manual_urls, manual_names = {}, {}, {}
    for target in manual:
        names = {normalize_name(target[name]) for name in ('name', 'venue_key')}
        target, reason = resolve_identity(target, ownership)
        if reason:
            notes[reason] += 1
        key = ident(target)
        for name in names | {normalize_name(target['venue_key'])}:
            manual_names.setdefault(name, set()).add(key)
        if key not in effective:
            effective[key] = {**target, 'urls': [], 'official_urls': [],
                              'domains': set(), 'scope_domains': set()}
            manual_urls[key] = set()
        base = effective[key]
        # Multiple manual rows may explicitly approve different domains. Only
        # manual input may extend this set, never a derived candidate.
        base['domains'].update(target['domains'])
        base['scope_domains'].update(target['domains'])
        if base['tavily'] is None:
            base['tavily'] = target['tavily']
        for url in target['urls']:
            canonical = canonical_requested_url(url, testing=testing)
            if canonical not in manual_urls[key] and domain_allowed(url, base['domains']):
                base['urls'].append(url)
                base['official_urls'].append(url)
                manual_urls[key].add(canonical)
    manual_keys = set(effective)
    suppressed = {ident(resolve_identity(t, ownership)[0]) for t in disabled}
    for target in derived:
        key = ident(target)
        if key in effective:
            base = effective[key]
            for url in target['urls']:
                if url in target.get('seed_urls', ()) or not domain_allowed(url, base['domains']):
                    notes['derived_domain_denied'] += 1
                elif (canonical := canonical_requested_url(url)) not in manual_urls[key]:
                    base['urls'].append(url)
                    base['official_urls'].append(url)
                    manual_urls[key].add(canonical)
        elif key in suppressed:
            notes['derived_suppressed'] += 1
        elif len(effective) >= MAX_TARGETS:
            notes['derived_truncated'] += 1
        else:
            effective[key] = target
    gap_keys = {ident(t) for t in derived}
    disabled_names = {normalize_name(t['venue_key']) for t in disabled}
    for target in seeds:
        key = ident(target)
        seed_only = key[0] not in ownership[0]
        name = normalize_name(target['name'])
        manual_matches = {key} if key in manual_keys else set()
        if seed_only and not manual_matches:
            manual_matches = manual_names.get(name, set())
        if manual_matches:
            if len(manual_matches) == 1:
                _add_seed_urls(effective[next(iter(manual_matches))], [target], manual=True, testing=testing)
            continue
        if key in gap_keys:
            continue
        if key in suppressed or (seed_only and name in disabled_names):
            notes['derived_suppressed'] += 1
        elif len(effective) >= MAX_TARGETS:
            notes['derived_truncated'] += 1
        else:
            effective[key] = target
    return list(effective.values())


def lead_allowed(url, domains, *, testing):
    return _https_or_test(url, testing=testing) and domain_allowed(url, domains)


def _resolve_lead(target, search, db, clock, *, testing):
    if not target['domains']:
        return None, 'tavily_unbounded'
    if search is None or db is None:
        return None, 'tavily_skipped'
    spec = target['tavily']
    try:
        outcome = search(db, spec['venue_id'], spec['field'], target='official_page', clock=clock)
    except Exception:
        return None, 'tavily_skipped'
    if getattr(outcome, 'status', '') != 'ok':
        reason = getattr(outcome, 'reason', '')
        return None, reason if _TAVILY_REASON.fullmatch(reason or '') else 'tavily_skipped'
    for lead in getattr(outcome, 'leads', ()) or ():
        url = getattr(lead, 'url', '')
        if lead_allowed(url, target['domains'], testing=testing):
            return url, None
    return None, 'tavily_skipped'


def resolve_jobs(targets, *, search, db, clock, testing):
    """Return fetch jobs and public skip notes. Unapproved lead URLs are not returned."""
    jobs, notes = [], []
    for target in targets:
        urls = list(target['urls'])
        official = target.get('official_urls', target['urls'])
        if not official and target['tavily']:
            chosen, reason = _resolve_lead(target, search, db, clock, testing=testing)
            if chosen:
                urls.append(chosen)
            else:
                notes.append({'url': '', 'disposition': reason})
        authorized = {url: canonical_requested_url(url, testing=testing) for url in official}
        fingerprint = target_fingerprint({**target, '_testing': testing}) if authorized else None
        for url in urls:
            identity = authorized.get(url)
            jobs.append({
                'venue_id': target['venue_id'],
                'venue_key': target['venue_key'],
                'name': target['name'],
                'url': url,
                'domains': target['domains'],
                'scope_domains': target.get('scope_domains', target['domains']),
                'source_authorization': ({'source_identity': identity, 'requested_url': identity,
                                          'final_url': identity, 'target_fingerprint': fingerprint}
                                         if identity else None),
                'seed_url': url in target.get('seed_urls', ()),
            })
    return jobs, notes
