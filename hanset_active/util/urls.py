"""Deterministic URL identity without DNS resolution or network access."""
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from .pii import strip_pii

DENIED = ('naver.com', 'naver.me', 'pstatic.net', 'naver.net', 'instagram.com', 'instagr.am')

def canonicalize(url: str, base: str | None = None) -> str:
    from urllib.parse import urljoin
    p = urlsplit(urljoin(base, url) if base else url)
    if p.scheme.lower() not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('Only anonymous HTTP URLs are supported')
    host = p.hostname.lower().encode('idna').decode()
    port = p.port
    authority = ('[' + host + ']' if ':' in host else host)
    if port and (p.scheme.lower(), port) not in (('http',80),('https',443)):
        authority += ':' + str(port)
    query = [(k,v) for k,v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('fbclid','gclid')]
    return strip_pii(urlunsplit((p.scheme.lower(), authority, p.path.rstrip('/') or '', urlencode(sorted(query)), '')))

def registrable_domain(url: str) -> str:
    host = (urlsplit(url).hostname if '://' in url else url).lower().rstrip('.')
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    parts = host.split('.')
    suffix = '.'.join(parts[-2:])
    return '.'.join(parts[-3:]) if suffix in ('co.kr','or.kr','go.kr','ac.kr','co.uk','com.au','co.jp') else suffix

def is_denied(url: str, denied_domains=DENIED) -> bool:
    host = (urlsplit(url).hostname or '').lower().rstrip('.')
    return any(host == d or host.endswith('.' + d) for d in denied_domains) or bool(re.search(r'(^|\.)naver\.', host))

def is_excluded(*values: str, terms=()) -> bool:
    if not terms:
        from ..config import load_profile
        terms = load_profile()['exclusion_terms']
    return any(t.casefold() in v.casefold() for t in terms for v in values if v)

def literal_private(url: str) -> bool:
    host = (urlsplit(url).hostname or '').lower().rstrip('.')
    if host == 'localhost' or host.endswith(('.localhost','.local','.internal')) or ('.' not in host and ':' not in host):
        return True
    try:
        ip = ipaddress.ip_address(host)
        if ip.version == 4 and ip in ipaddress.ip_network('198.18.0.0/15'):
            return False
        return not ip.is_global
    except ValueError:
        return False
