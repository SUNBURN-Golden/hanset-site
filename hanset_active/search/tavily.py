"""One bounded POST, private credentials, and sanitized unverified search leads."""
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import unquote, urlsplit

import requests

from .budget import BudgetSnapshot, locked_budget
from ..util.pii import strip_pii
from ..util.timeutil import kst_now
from ..util.urls import canonicalize, is_denied, is_excluded, literal_private

_KEY_PATH = Path('/workspace/.secrets/tavily_key')
ENDPOINT = 'https://api.tavily.com/search'
MAX_QUERY = 240
MAX_RESPONSE = 128 * 1024


@dataclass(frozen=True)
class SearchLead:
    url: str
    title: str
    snippet: str
    handle: str | None = None
    verification: str = 'unverified'


@dataclass(frozen=True)
class SearchOutcome:
    status: str
    reason: str
    venue_id: str | None = None
    gap_field: str | None = None
    leads: tuple[SearchLead, ...] = ()
    budget: BudgetSnapshot | None = None


@dataclass(frozen=True)
class _Credential:
    value: str = field(default='', repr=False)
    reason: str = 'ok'


def _load_credential():
    """Only the fixed file or existing process env; never include raw errors."""
    try:
        try:
            fd = os.open(_KEY_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            # A dangling symlink is an existing insecure file, not an absent key.
            if _KEY_PATH.is_symlink():
                return _Credential(reason='tavily_key_invalid')
            value = os.environ.get('TAVILY_API_KEY', '')
            from_file = False
        else:
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_size > 4096):
                    return _Credential(reason='tavily_key_invalid')
                value = stream.read(4097).decode('ascii')
            from_file = True
        value = value.strip()
        if not value and not from_file:
            return _Credential(reason='tavily_key_missing')
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,256}', value):
            return _Credential(reason='tavily_key_invalid')
        if from_file:
            os.environ['TAVILY_API_KEY'] = value
        return _Credential(value)
    except Exception:
        return _Credential(reason='tavily_key_invalid')


def _decoded(value):
    for _ in range(4):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    return value


def _clean_text(value, secret, limit):
    if not isinstance(value, str):
        return ''
    # Redact before truncation/PII filtering, including percent-encoded echoes.
    text = re.sub(r'<[^>]*>', '', _decoded(value[:MAX_RESPONSE]))
    text = strip_pii(text.replace(secret, '[redacted]'))
    text = ' '.join(''.join(c if c.isprintable() else ' ' for c in text).split())
    return text.replace(secret, '[redacted]')[:limit]


_IG_RESERVED = frozenset({
    'p', 'reel', 'reels', 'tv', 'stories', 'explore', 'accounts', 'login',
    'direct', 'about', 'legal', 'privacy', 'terms', 'developer', 'developers',
    'directory', 'challenge', 'web', 'share', 'oauth', 'graphql', 'api',
})
_CREDENTIAL_URL = re.compile(r'(?i)(?:api[_-]?key|token|secret|session|sid|auth|password|credential)\s*[=/]')


def _lead(row, target, secret):
    if not isinstance(row, dict) or not isinstance(row.get('url'), str):
        return None
    raw = row['url']
    if len(raw) > 2048:
        return None
    decoded = _decoded(raw)
    if (secret in decoded or secret in raw or _CREDENTIAL_URL.search(decoded)
            or strip_pii(decoded) != decoded or re.search(r'[\s\\\x00-\x1f\x7f]', decoded)):
        return None
    try:
        parts = urlsplit(raw)
        # Reject userinfo, fragments and unusual ports even before canonicalization.
        if parts.username or parts.password or parts.fragment or parts.port not in (None, 80, 443):
            return None
        url = canonicalize(raw)
        candidate_text = [row.get(key, '') for key in ('title', 'content')]
        if literal_private(url) or is_excluded(decoded, *(v for v in candidate_text if isinstance(v, str))):
            return None
        handle = None
        if target == 'instagram_handle':
            if parts.scheme != 'https' or parts.hostname not in ('instagram.com', 'www.instagram.com') or parts.query or parts.port:
                return None
            # Match the original path so encoded separators/usernames cannot bypass it.
            match = re.fullmatch(r'/([A-Za-z0-9_](?:[A-Za-z0-9_.]{0,28}[A-Za-z0-9_])?)/?', parts.path)
            if not match or match[1].lower() in _IG_RESERVED or '..' in match[1]:
                return None
            handle = match[1].lower()
            url = 'https://www.instagram.com/' + handle + '/'
        elif is_denied(url):
            return None
        if secret in url or (handle and secret in handle):
            return None
        return SearchLead(url, _clean_text(row.get('title', ''), secret, 160),
                          _clean_text(row.get('content', ''), secret, 320), handle)
    except (ValueError, TypeError, UnicodeError):
        return None


def _retry_after(value, clock):
    try:
        seconds = int(value)
    except (ValueError, TypeError, OverflowError):
        try:
            parsed = parsedate_to_datetime(value)
            seconds = int((parsed - kst_now(clock)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            seconds = 60
    return min(3600, max(1, seconds))


def _request(query, target, credential, reservation, *, clock=None, transport=None):
    session = None
    response = None
    try:
        if transport is None:
            session = requests.Session()
            session.trust_env = False
            # requests' default adapter has no retries; set it explicitly as well.
            session.mount('https://', requests.adapters.HTTPAdapter(max_retries=0))
            transport = session.post
        # Check after session setup, immediately before invoking the transport.
        try:
            if not reservation.ready_to_dispatch():
                return reservation.reason, (), 0
        except Exception:
            return 'tavily_budget_unavailable', (), 0
        response = transport(
            ENDPOINT,
            headers={'Authorization': 'Bearer ' + credential.value},
            json={'query': query, 'search_depth': 'basic', 'auto_parameters': False,
                  'topic': 'general', 'max_results': 3, 'include_answer': False,
                  'include_raw_content': False, 'include_images': False},
            timeout=(5, 10), allow_redirects=False, stream=True,
        )
        if response.status_code == 429:
            return 'tavily_rate_limited', (), _retry_after(response.headers.get('Retry-After'), clock)
        if response.status_code != 200:
            return 'tavily_http_error', (), 0
        body = bytearray()
        deadline = time.monotonic() + 30
        for chunk in response.iter_content(chunk_size=8192):
            if time.monotonic() > deadline:
                return 'tavily_timeout', (), 0
            if len(body) + len(chunk) > MAX_RESPONSE:
                return 'tavily_response_too_large', (), 0
            body.extend(chunk)
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
                return 'tavily_invalid_response', (), 0
            leads = []
            for row in payload['results'][:3]:
                lead = _lead(row, target, credential.value)
                if lead and lead.url not in {item.url for item in leads}:
                    leads.append(lead)
            return 'ok', tuple(leads), 0
        except (ValueError, TypeError, RecursionError):
            return 'tavily_invalid_response', (), 0
    except requests.Timeout:
        return 'tavily_timeout', (), 0
    except Exception:
        # Never interpolate or propagate requests exceptions (they can carry auth).
        return 'tavily_transport_error', (), 0
    finally:
        # Even cleanup errors from transports must not escape with credentials.
        for resource in (response, session):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass


def _search(query, target, *, clock=None, transport=None):
    """Internal: discovery must validate venue identity before calling this."""
    credential = _load_credential()
    if credential.reason != 'ok':
        return SearchOutcome('skipped', credential.reason)
    snapshot = None
    try:
        with locked_budget(clock=clock) as reservation:
            if not reservation.reserve():
                return SearchOutcome('skipped', reservation.reason, budget=reservation.snapshot)
            snapshot = reservation.snapshot
            reason, leads, cooldown = _request(query, target, credential, reservation, clock=clock, transport=transport)
            snapshot = reservation.snapshot
            if reason in {'tavily_budget_unavailable', 'tavily_budget_exhausted', 'tavily_cooldown', 'tavily_clock_changed'}:
                return SearchOutcome('skipped', reason, budget=snapshot)
            reservation.finish(reason, cooldown)
            return SearchOutcome('ok' if reason == 'ok' else 'error', reason, leads=leads, budget=snapshot)
    except Exception:
        return SearchOutcome('skipped', 'tavily_budget_unavailable', budget=snapshot)
