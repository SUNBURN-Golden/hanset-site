"""Filesystem-only inputs and artifact checks for managed HANSET releases."""

import csv
import hashlib
import ipaddress
import json
import os
import re
import tempfile
import unicodedata
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

CSV_FIELDS = {
    'boxes.csv': ('name', 'confidence', 'price_source_url', 'website', 'cf_listing_url',
                  'address', 'drop_in_price_krw', 'booking_url', 'official_check_date',
                  'cf_official_check', 'cf_official_url', 'hyrox_official_check',
                  'hyrox_official_url', 'last_verified', 'city'),
    'saunas.csv': ('name', 'confidence', 'source_url', 'price_krw', 'website', 'last_verified', 'city'),
    'events.csv': ('name', 'confidence', 'start_date', 'end_date', 'last_verified'),
}
CONTENT = 'site/content/'
PUBLIC_JSON = tuple(CONTENT + name + '.json' for name in (
    'ads', 'venues', 'venues_en', 'events', 'events_en', 'foreigner', 'closed_days', 'closed_month',
)) + ('data/verified/events.json', 'data/verified/creators.json')
PROJECTED_FILES = frozenset(CONTENT + name + '.json' for name in (
    'venues', 'venues_en', 'closed_days',
)) | {'boxes.csv', 'saunas.csv'}
HEX = re.compile(r'[0-9a-f]{64}')
SLUG = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*')
DECLINED_BRAND = re.compile(r'runport|런포트|버핏|butfitclubhouse', re.I)
PERMANENT = re.compile(r'permanently closed|permanent closure|영구\s*(?:폐업|폐쇄)|폐업', re.I)


class GateError(Exception):
    def __init__(self, gate, reason):
        self.gate, self.reason = gate, reason
        super().__init__(gate, reason)


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_json(path, value):
    atomic_write(path, canonical(value))


def atomic_write(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        tmp = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            tmp.replace(path)
            sync_dir(path.parent)
        finally:
            tmp.unlink(missing_ok=True)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def data_hash(root):
    files = {}
    for name in PUBLIC_JSON:
        path = root / name
        if path.is_file():
            value = read_json(path)
            if name in (CONTENT + 'venues.json', CONTENT + 'events.json'):
                value = [(key, record) for key, record in value.items() if not key.startswith('_')]
            files[name] = value
    venues = files[CONTENT + 'venues.json']
    for name, fields in CSV_FIELDS.items():
        keys = (dict(files[CONTENT + 'events.json']) if name == 'events.csv' else
                {key for key, row in venues if row['csv'] + '.csv' == name})
        with (root / name).open(encoding='utf-8-sig', newline='') as stream:
            rows = [{field: row.get(field, '') for field in fields}
                    for row in csv.DictReader(stream) if row['name'] in keys]
        files[name] = sorted(rows, key=canonical)
    for pattern in ('pages/*.html', 'articles/*.md'):
        for path in (root / CONTENT).glob(pattern):
            files[path.relative_to(root).as_posix()] = path.read_text(encoding='utf-8')
    return digest(canonical(files))


def iso_date(value):
    try:
        result = date.fromisoformat(value)
        return result if result.isoformat() == value else None
    except (ValueError, TypeError):
        return None


def web_host(value):
    if not isinstance(value, str) or re.search(r'[\s\\\x00-\x1f\x7f<>"{}|^`]', value):
        return None
    try:
        url = urlsplit(value)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username is not None or url.password is not None:
            return None
        if url.port == 0:
            return None
        host = url.hostname.rstrip('.').lower()
        if '%' in host or host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if '.' not in host:
                return None
        return host
    except ValueError:
        return None


def consent_registry(path, today):
    """Load versioned, exact branch decisions reviewed by operations."""
    try:
        if (any(entry.is_symlink() for entry in (path, *path.parents))
                or not path.is_file() or path.stat().st_size > 100_000):
            raise ValueError
        registry = read_json(path)
        if set(registry) != {'schema_version', 'venues'} or type(registry['schema_version']) is not int or registry['schema_version'] != 1:
            raise ValueError
        rows = registry['venues']
        if not isinstance(rows, list) or not rows:
            raise ValueError
        identifiers, runport = set(), False
        for row in rows:
            if (set(row) != {'venue_id', 'venue_key', 'slugs', 'names', 'aliases',
                             'official_urls', 'decision', 'decided_on'}
                    or row['decision'] not in ('allowed', 'declined')
                    or not (iso_date(row['decided_on']) or date.max) <= today):
                raise ValueError
            for key in ('venue_id', 'venue_key'):
                if not isinstance(row[key], str) or not identity(row[key]):
                    raise ValueError
            for key in ('slugs', 'names', 'aliases', 'official_urls'):
                values = row[key]
                if not isinstance(values, list) or not all(isinstance(item, str) and identity(item) for item in values):
                    raise ValueError
            if (not row['names'] or not row['slugs'] or not row['official_urls']
                    or not all(SLUG.fullmatch(item) for item in row['slugs'])
                    or not all(web_host(item) for item in row['official_urls'])):
                raise ValueError
            keys = {identity(item) for item in (row['venue_id'], row['venue_key'], *row['slugs'],
                                                  *row['names'], *row['aliases'])}
            # Shared hosts are valid for related branches. A URL path is an
            # identifier only when it names that exact branch.
            keys.update(('url', _official_identity(url)) for url in row['official_urls'])
            if identifiers & keys:
                raise ValueError
            identifiers.update(keys)
            if (row['decision'] == 'declined' and row['decided_on'] == '2026-10-19'
                    and 'runport-mapo' in row['slugs']
                    and any(DECLINED_BRAND.search(identity(item))
                            for item in (*row['names'], *row['aliases']))):
                runport = True
        if not runport:
            raise ValueError
        return rows
    except (AttributeError, KeyError, TypeError, OSError, ValueError, RecursionError):
        raise GateError('consent', 'consent_missing_or_invalid') from None


def _official_identity(url):
    value = urlsplit(url)
    return (web_host(url).removeprefix('www.'), value.path.rstrip('/').casefold(), value.query)


def identity(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())


def excluded_venue(registry, names, urls):
    names = {identity(name) for name in names if isinstance(name, str)}
    official = {_official_identity(url) for url in urls if web_host(url)}
    if any(DECLINED_BRAND.search(name) for name in names) or any(DECLINED_BRAND.search(identity(host)) for host, _, _ in official):
        return True
    for row in registry:
        if row['decision'] != 'declined':
            continue
        aliases = [row['venue_id'], row['venue_key'], *row['names'], *row['aliases'], *row['slugs']]
        rejected = {_official_identity(url) for url in row['official_urls']}
        if names.intersection(map(identity, aliases)) or official & rejected:
            return True
        if any(host == other and (not path or old_path == path or old_path.startswith(path + '/'))
               and (not query or query == old_query)
               for other, old_path, old_query in official for host, path, query in rejected):
            return True
    return False


def permanent_closure(text):
    return isinstance(text, str) and bool(PERMANENT.fullmatch(text.strip().rstrip('.')))


def closure_slugs_from_records(records, today):
    # An exact, open-ended, officially dated permanent closure can explain a removal.
    return {slug for slug, row in records.items()
            if isinstance(row, dict) and permanent_closure(row.get('closed')) and row.get('until') in (None, '')
            and row.get('source') in ('Official page', 'Official closure notice', 'Official notice', 'Official Instagram')
            and web_host(row.get('url')) and (checked := iso_date(row.get('checked'))) and checked <= today}


def artifact_manifest(dist, applied_data_sha256):
    artifacts = {}
    if dist.is_dir() and not dist.is_symlink():
        for path in sorted(dist.rglob('*')):
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise GateError('sanity', 'invalid_artifact')
            if path.is_file():
                with path.open('rb') as stream:
                    artifacts[path.relative_to(dist).as_posix()] = {
                        'sha256': hashlib.file_digest(stream, 'sha256').hexdigest(),
                        'bytes': os.fstat(stream.fileno()).st_size,
                    }
    return manifest_for(artifacts, applied_data_sha256)


def manifest_for(artifacts, applied_data_sha256):
    pages = {name: item['sha256'] for name, item in artifacts.items()
             if name.endswith('.html') and not name.startswith('assets/')}
    venues = sorted(match[1] for name in pages if (match := re.fullmatch(r'finder/([a-z0-9-]+)/index\.html', name)))
    return {'schema_version': 1, 'applied_data_sha256': applied_data_sha256,
            'pages': pages, 'page_count': len(pages), 'venue_slugs': venues, 'venue_count': len(venues),
            'total_bytes': sum(item['bytes'] for item in artifacts.values()), 'artifacts': artifacts}


def require_home(manifest):
    if not manifest['artifacts'].get('index.html', {}).get('bytes'):
        raise GateError('sanity', 'empty_build')


def validate_manifest(manifest):
    try:
        artifacts = manifest['artifacts']
        data = manifest['applied_data_sha256']
        if (data is not None and not HEX.fullmatch(data)) or not isinstance(artifacts, dict):
            raise ValueError
        for name, item in artifacts.items():
            part = PurePosixPath(name)
            if (part.is_absolute() or '..' in part.parts or part.as_posix() != name or name == '.'
                    or not isinstance(item, dict) or set(item) != {'sha256', 'bytes'}
                    or not HEX.fullmatch(item['sha256']) or type(item['bytes']) is not int or item['bytes'] < 0):
                raise ValueError
        if canonical(manifest_for(artifacts, data)) != canonical(manifest):
            raise ValueError
        require_home(manifest)
        return manifest
    except (OSError, ValueError, KeyError, TypeError, GateError, RecursionError):
        raise GateError('baseline', 'missing_or_corrupt') from None


def read_manifest(path):
    try:
        return validate_manifest(read_json(path))
    except (OSError, ValueError, TypeError, RecursionError):
        raise GateError('baseline', 'missing_or_corrupt') from None


def sanity(previous, candidate, closed):
    require_home(candidate)
    drop = previous['page_count'] - candidate['page_count']
    if drop * 100 > previous['page_count'] * 5 or drop > 20:
        raise GateError('sanity', 'page_count_drop')
    if set(previous['venue_slugs']) - set(candidate['venue_slugs']) - closed:
        raise GateError('sanity', 'venue_removed_without_closure')
    if candidate['total_bytes'] * 2 < previous['total_bytes']:
        raise GateError('sanity', 'size_drop')
