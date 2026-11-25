"""Explicit local approval registry; prose in legacy sources is never executable policy."""
import json
import os
import math
import datetime as dt
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit, quote, unquote

from hanset_active.crawl_policy import is_korean_news, is_naver
from hanset_active.util.urls import is_collect_denied

REPO = Path(__file__).resolve().parents[1]


class Rejected(ValueError):
    """Safe reason code only: never include page data or credentials in errors."""


def external_path(path):
    path = Path(path).expanduser().resolve()
    if path == REPO or REPO in path.parents or any((p / '.git').exists() for p in path.parents):
        raise Rejected('state_inside_repository')
    return path


def state_path():
    return external_path(Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'hanset-collector')


def load(path=None):
    cfg = json.loads(Path(__file__).with_name('config.json').read_text())
    exclusions = {key: list(cfg[key]) for key in ('excluded_terms', 'excluded_venues')}
    if path:
        cfg.update(json.loads(external_path(path).read_text()))
    for key, mandatory in exclusions.items():
        if not isinstance(cfg[key], list) or not all(isinstance(x, str) and x for x in cfg[key]):
            raise Rejected('invalid_exclusions')
        cfg[key] = list(dict.fromkeys(mandatory + cfg[key]))
    for key in ('daily_candidates', 'daily_new_bodies', 'daily_attempts', 'max_depth',
                'lease_seconds', 'max_requests', 'max_response_bytes', 'max_total_bytes',
                'request_timeout', 'max_redirects'):
        if type(cfg[key]) is not int or cfg[key] <= 0:
            raise Rejected('invalid_config')
    if (cfg['max_depth'] > 2 or type(cfg['domain_concurrency']) is not int or cfg['domain_concurrency'] != 1
            or any(type(cfg[key]) not in (int, float) or not math.isfinite(cfg[key]) or cfg[key] < 0
                   for key in ('min_interval', 'global_gap'))):
        raise Rejected('unsafe_config')
    for p in cfg['policies']:
        u = urlsplit(p['origin'])
        if u.scheme != 'https' or not u.hostname or u.port not in (None, 443) or u.path or u.query or u.username:
            raise Rejected('invalid_policy_origin')
        if not p.get('public') or not p.get('paths') or (
                is_korean_news(p['origin']) and not p.get('robots_reviewed_at')):
            raise Rejected('incomplete_policy')
        if 'robots_reviewed_at' in p:
            try:
                dt.date.fromisoformat(p['robots_reviewed_at'])
            except (ValueError, TypeError):
                raise Rejected('invalid_robots_review_date') from None
        if any(not x.startswith('/') for x in p['paths']):
            raise Rejected('invalid_policy_path')
        delay = p.get('crawl_delay', 0)
        if not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < 0:
            raise Rejected('invalid_crawl_delay')
        interval = p.get('recheck_seconds', 86400)
        if type(interval) is not int or interval < 300:
            raise Rejected('invalid_recheck_interval')
    if any(not isinstance(x, str) or not x.startswith('chrome-extension://') or
           '/' in x[len('chrome-extension://'):] for x in cfg['extension_origins']):
        raise Rejected('invalid_extension_origin')
    # Registry values are operator input, never inferred from discovered text.
    for seed in cfg['seeds']:
        seed_policy(cfg, seed, check_freshness=False)
    return cfg


def policy_path(value):
    return quote(unicodedata.normalize('NFKC', unquote(unquote(value))), safe="/:@!$&'()*+,;=-._~")


def _origin_key(url):
    host = (url.hostname or '').lower().rstrip('.').encode('idna').decode('ascii')
    return url.scheme, host, url.port or 443


def _review_is_fresh(policy, now):
    try:
        reviewed = dt.date.fromisoformat(policy.get('robots_reviewed_at'))
    except (TypeError, ValueError):
        return False
    # Operators date robots reviews in KST.
    today = dt.datetime.fromtimestamp(time.time() if now is None else now,
                                      dt.timezone(dt.timedelta(hours=9))).date()
    return 0 <= (today - reviewed).days <= 90


def browser_policy_for_origin(cfg, origin, now=None):
    """Check eligibility to register browser consent for a canonical HTTPS origin."""
    key = _origin_key(urlsplit(origin))
    reason = 'access_not_approved'
    if is_collect_denied(origin):
        raise Rejected(reason)
    for p in cfg['policies']:
        if _origin_key(urlsplit(p['origin'])) == key and p.get('public') and p.get('browser'):
            if p.get('paths') and (not is_korean_news(origin) or _review_is_fresh(p, now)):
                return p
            reason = 'robots_review_stale'
    raise Rejected(reason)


def _matching_policy(cfg, url):
    from .privacy_filter import safe_url
    u = urlsplit(safe_url(url))
    if is_collect_denied(url):
        return None, u.path
    key = _origin_key(u)
    for p in cfg['policies']:
        paths = [policy_path(x) for x in p['paths']]
        if _origin_key(urlsplit(p['origin'])) == key and any(
                u.path == x or u.path.startswith(x if x.endswith('/') else x + '/') for x in paths):
            return p, u.path
    return None, u.path


def _disallowed(policy, path):
    return any(path.startswith(policy_path(x)) for x in policy.get('disallow', []))


def seed_policy(cfg, url, *, now=None, check_freshness=True):
    """Check an explicit seed; config loading validates coverage independently of age."""
    policy, path = _matching_policy(cfg, url)
    news = is_korean_news(url)
    if not policy or not policy.get('public') or (news and _disallowed(policy, path)):
        raise Rejected('access_not_approved')
    if news and check_freshness and not _review_is_fresh(policy, now):
        raise Rejected('robots_review_stale')
    return policy


def policy_for(cfg, url, browser=False, now=None, required=True):
    policy, path = _matching_policy(cfg, url)
    reason = 'access_not_approved'
    if policy and policy.get('public') and (not browser or policy.get('browser')):
        news = is_korean_news(url)
        if news and not _review_is_fresh(policy, now):
            reason = 'robots_review_stale'
        elif not news or not _disallowed(policy, path):
            if policy.get('official_venues') and (news or is_naver(url)):
                return {**policy, 'official_venues': []}
            return policy
    if required:
        raise Rejected(reason)
    return {}  # Manual DOM clips may be clues; this never grants network access.
