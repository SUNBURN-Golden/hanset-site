"""Explicit local approval registry; prose in legacy sources is never executable policy."""
import json
import os
import math
import datetime as dt
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit, quote, unquote

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
    if (cfg['max_depth'] > 2 or not isinstance(cfg['min_interval'], (int, float))
            or not math.isfinite(cfg['min_interval']) or cfg['min_interval'] < 5
            or cfg['domain_concurrency'] != 1
            or any(cfg[k] > cap for k, cap in [('daily_candidates', 500),
                                              ('daily_new_bodies', 200), ('daily_attempts', 200)])):
        raise Rejected('unsafe_config')
    for p in cfg['policies']:
        u = urlsplit(p['origin'])
        if u.scheme != 'https' or not u.hostname or u.port not in (None, 443) or u.path or u.query or u.username:
            raise Rejected('invalid_policy_origin')
        if not p.get('public') or not p.get('paths') or not p.get('robots_reviewed_at'):
            raise Rejected('incomplete_policy')
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
        policy_for(cfg, seed)
    return cfg


def policy_path(value):
    return quote(unicodedata.normalize('NFKC', unquote(unquote(value))), safe="/:@!$&'()*+,;=-._~")


def policy_for(cfg, url, browser=False, now=None, required=True):
    from .privacy_filter import safe_url
    url = safe_url(url)
    u = urlsplit(url)
    for p in cfg['policies']:
        origin = urlsplit(p['origin'])
        host = (origin.hostname or '').lower().rstrip('.').encode('idna').decode('ascii')
        paths = [policy_path(x) for x in p['paths']]
        if (origin.scheme, host, origin.port or 443) == (u.scheme, u.hostname, u.port or 443) and any(
                u.path == x or u.path.startswith(x if x.endswith('/') else x + '/') for x in paths):
            if not p.get('public') or (browser and not p.get('browser')):
                break
            reviewed = dt.date.fromisoformat(p['robots_reviewed_at'])
            # Operators date robots reviews in KST; a UTC date rejected same-morning reviews before 09:00 KST.
            today = dt.datetime.fromtimestamp(time.time() if now is None else now, dt.timezone(dt.timedelta(hours=9))).date()
            if not 0 <= (today - reviewed).days <= 90:
                if required:
                    raise Rejected('robots_review_stale')
                return {}
            if any(u.path.startswith(policy_path(x)) for x in p.get('disallow', [])):
                break
            return p
    if required:
        raise Rejected('access_not_approved')
    return {}  # Manual DOM clips may be clues; this never grants network access.
