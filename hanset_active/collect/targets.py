"""Approved official URL lists. Search leads are not fetched unless the domain was approved."""
import json
import re
from urllib.parse import urlsplit

from hanset_active.util.urls import is_denied, is_excluded, literal_private, registrable_domain

from . import CollectError
from .config import MAX_TARGETS, domain_allowed, official_domains

_TAVILY_REASON = re.compile(r'tavily_[a-z_]{1,40}')


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


def parse_targets(path, *, testing):
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
            })
    return jobs, notes
