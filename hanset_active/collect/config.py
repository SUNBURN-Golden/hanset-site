"""External paths, model identity, and official-source identity checks."""
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from hanset_active.config import REPO_ROOT, load_profile
from hanset_active.crawl_policy import NAVER_DOMAINS, is_korean_news
from hanset_active.util import followups
from hanset_active.util.urls import is_collect_denied, is_denied, is_excluded, registrable_domain

from . import CollectError

DEEPSEEK_MODELS = frozenset({'deepseek-flash', 'deepseek-v4-pro'})
QWEN_MODEL = 'qwen4-max-preview'
PLAN_MODELS = DEEPSEEK_MODELS | {QWEN_MODEL}
ALLOWED_MODELS = PLAN_MODELS
DEFAULT_MODEL = 'deepseek-flash'
SUBPAGE_MAX = 2
BLOCK_COOLDOWN_DAYS = 14
UNCHANGED_COOLDOWN_DAYS = 7
IG_READ_MAX = 20
IG_READ_TARGET = 10
MAX_TEXT = 4_000
MAX_TARGETS = 100
HANDLE_RE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._]{0,29}')
RESERVED_HANDLES = frozenset({
    'p', 'reel', 'reels', 'accounts', 'explore', 'direct', 'stories',
    'challenge', 'checkpoint', 'about', 'login',
})
SHARED_WEB_DOMAINS = frozenset({
    'airbnb.com', 'blogger.com', 'blogspot.com', 'daum.net', 'expedia.com', 'expedia.co.kr',
    'foursquare.com', 'g.page', 'goo.gl',
    'google.co.kr', 'google.com', 'hotels.com', 'kakao.com', 'kayak.com', 'openstreetmap.org',
    'trip.com', 'trivago.com', 'yelp.com',
}) | NAVER_DOMAINS

_SHARED_HOST = re.compile(r'(?:^|\.)(?:blog|blogs|directory|directories|listings?|ota|maps?|reviews?|news)(?:\.|$)', re.I)

def outside(path):
    resolved = Path(path).resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise CollectError('runtime_inside_checkout')
    return resolved


def configured_model(value=None):
    model = value if value is not None else os.environ.get('HANSET_DEEPSEEK_MODEL', DEFAULT_MODEL)
    if model not in DEEPSEEK_MODELS:
        raise CollectError('model_config_invalid')
    return model


@dataclass
class CollectSettings:
    data_dir: Path | None = None
    active_dir: Path | None = None
    twin_dir: Path | None = None
    model: str | None = None

    def __post_init__(self):
        self.data_dir = outside(self.data_dir or os.environ.get('HANSET_IG_DIR', '/workspace/hanset-data/ig'))
        self.active_dir = outside(self.active_dir or os.environ.get('HANSET_ACTIVE_DIR', '/workspace/hanset-data/active'))
        self.twin_dir = outside(self.twin_dir or os.environ.get('HANSET_TWIN_DIR', '/workspace/hanset-data/twin'))
        self.model = configured_model(self.model)

    @property
    def inbox(self):
        return self.data_dir / 'inbox'


def handle_valid(value):
    return isinstance(value, str) and bool(HANDLE_RE.fullmatch(value)) and value.lower() not in RESERVED_HANDLES


def canonical_instagram(value, *, handle=None):
    """Official post, reel, or profile URL. No query, fragment, or login route."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or parsed.netloc not in ('instagram.com', 'www.instagram.com'):
            raise ValueError
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError
        parts = [part for part in parsed.path.split('/') if part]
        if len(parts) == 1 and handle_valid(parts[0]):
            if handle and parts[0].lower() != handle.lower():
                raise ValueError
            kind = 'profile'
        elif len(parts) == 2 and parts[0] in ('p', 'reel') and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', parts[1]):
            kind = parts[0]
        else:
            raise ValueError
        url = 'https://www.instagram.com/' + '/'.join(parts) + '/'
        return url, kind
    except (TypeError, ValueError):
        raise CollectError('invalid_source_url') from None


def official_site(url, *, profile=None):
    profile = load_profile() if profile is None else profile
    host = urlsplit(url).hostname
    if (not host or is_korean_news(url) or _SHARED_HOST.search(host) or is_denied(url, profile['denied_domains']) or is_denied(url)
            or is_excluded(url, terms=profile['exclusion_terms'])):
        return False
    shared = followups.portal_domains(profile) | SHARED_WEB_DOMAINS | set(profile['reservation_platform_domains'])
    shared |= set(profile['sns_domains'])
    return (registrable_domain(url) not in shared
            and not any(host.startswith(pattern.removesuffix('*'))
                        for pattern in profile['aggregator_domains'] if pattern.endswith('*')))


def official_domains(entry):
    domains = set()
    for raw in entry.get('official_domains') or []:
        if not isinstance(raw, str) or not raw or len(raw) > 253:
            raise CollectError('targets_invalid')
        host = raw.lower().rstrip('.')
        authority = f'[{host}]' if ':' in host else host
        if not re.fullmatch(r'[a-z0-9.:-]+', host) or not official_site('https://' + authority):
            raise CollectError('targets_invalid')
        domains.add(host)
    return domains


def domain_allowed(url, domains):
    return scope_allowed(url, domains) and official_site(url)


def scope_allowed(url, scope_domains):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme in ('https', 'http') and parsed.hostname is not None and not is_collect_denied(url)
                and parsed.username is None and parsed.password is None and registrable_domain(url) in scope_domains)
    except (TypeError, ValueError, AttributeError):
        return False
