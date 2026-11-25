"""Optional, run-scoped egress for public web collection."""
from dataclasses import dataclass, field
import ipaddress
import json
import logging
import os
import shlex
import socket
import subprocess
import threading
from contextlib import nullcontext
from urllib.parse import urlsplit

from .locks import host_lock, hostname
from hanset_active.util.timeutil import Clock
from hanset_active.util.urls import is_collect_denied

ENV = 'HANSET_WEB_PROXY'
EXIT_ENV = 'HANSET_WEB_PROXY_EXIT_IP'
CHECK_ENV = 'HANSET_WEB_PROXY_CHECK_URL'
HEAL_ENV = 'HANSET_WEB_PROXY_HEAL_CMD'
DEFAULT_CHECK_URL = 'https://api.ipify.org'
# Heavy resources never pass through the metered proxy.
HEAVY_TYPES = ('image/', 'font/', 'audio/', 'video/', 'application/font', 'application/x-font')
HEAVY_RESOURCES = frozenset({'image', 'font', 'media', 'imageset', 'texttrack', 'manifest'})


FALLBACK_REASONS = ('proxy_unavailable', 'proxy_exit_mismatch')
FALLBACK_PHASES = ('preflight', 'mid_run')
FALLBACK_EVENT = 'proxy_fallback_direct'


def log_event(event):
    logging.getLogger(__name__).warning('%s', json.dumps(event))


@dataclass
class Egress:
    mode: str = 'direct'
    reason: str = ''
    phase: str = 'unset'
    proxy: str = field(default='', repr=False)
    _emitted: bool = field(default=False, init=False, repr=False)
    _lock: object = field(default_factory=threading.Lock, init=False, repr=False)

    def request_proxy(self):
        with self._lock:
            return self.proxy if self.mode == 'proxy' else ''

    def fallback(self, reason, phase, log=log_event):
        if reason not in FALLBACK_REASONS or phase not in FALLBACK_PHASES:
            raise ValueError('invalid_egress_fallback')
        with self._lock:
            if self._emitted:
                return False
            self.mode, self.reason, self.phase, self.proxy = 'direct', reason, phase, ''
            self._emitted = True
        log({'event': FALLBACK_EVENT, 'phase': phase, 'reason': reason})
        return True

    def public(self):
        with self._lock:
            if self._emitted:
                return {'event': FALLBACK_EVENT, 'mode': 'direct',
                        'reason': self.reason, 'phase': self.phase}
        return None


def resolve_run(proxy_setting=None, *, clock, host_dir, log=log_event):
    """Return the run egress, a terminal reason (if any), and heal diagnostics."""
    try:
        proxy = configured() if proxy_setting is None else configured({ENV: proxy_setting})
    except ValueError:
        return None, 'proxy_invalid', new_heal_status()
    if not proxy:
        return Egress(), '', new_heal_status()
    reason, heal = preflight_with_retry(proxy, clock=clock, host_dir=host_dir)
    egress = Egress(mode='proxy', proxy=proxy)
    if reason in FALLBACK_REASONS:
        egress.fallback(reason, 'preflight', log)
        return egress, '', heal
    return (None if reason else egress), reason, heal


def parse(value):
    """Return (scheme, host, port) for a loopback SOCKS5 URL, else raise ValueError."""
    parts = urlsplit(value or '')
    if (parts.scheme not in ('socks5', 'socks5h') or not parts.hostname
            or parts.username is not None or parts.password is not None or parts.query or parts.fragment):
        raise ValueError('invalid proxy')
    host = parts.hostname
    if host != 'localhost' and not ipaddress.ip_address(host).is_loopback:
        raise ValueError('proxy must be loopback')
    if not parts.port or parts.path not in ('', '/'):
        raise ValueError('invalid proxy')
    return parts.scheme, host, parts.port


def configured(environ=None):
    """The validated proxy URL, '' when unset. Invalid values raise ValueError."""
    value = (environ if environ is not None else os.environ).get(ENV, '').strip()
    if not value:
        return ''
    _, host, port = parse(value)
    # Remote DNS keeps lookups on the proxy side too.
    authority = f'[{host}]' if ':' in host else host
    return f'socks5h://{authority}:{port}'


def browser_server(proxy):
    _, host, port = parse(proxy)
    return f'socks5://{host}:{port}'


def reachable(proxy, timeout=3.0):
    _, host, port = parse(proxy)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def is_heavy(content_type):
    return (content_type or '').lower().startswith(HEAVY_TYPES)


def preflight(proxy, *, environ=None, session=None, timeout=10.0, host_dir=None):
    """Return '' when the proxy may be used, else a short reason code."""
    env = environ if environ is not None else os.environ
    expected = env.get(EXIT_ENV, '').strip()
    check_url = env.get(CHECK_ENV, '').strip() or DEFAULT_CHECK_URL
    if expected:
        try:
            expected = ipaddress.ip_address(expected)
            parsed = urlsplit(check_url)
            if (parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.port == 0
                    or parsed.username is not None or parsed.password is not None or is_collect_denied(check_url)):
                return 'proxy_invalid'
        except ValueError:
            return 'proxy_invalid'
    if not reachable(proxy):
        return 'proxy_unavailable'
    if not expected:
        return ''
    import requests
    from .fetcher import USER_AGENT
    own = session is None
    session = session or requests.Session()
    session.trust_env = False
    try:
        lock = host_lock(host_dir, hostname(check_url)) if host_dir is not None else nullcontext()
        with lock:
            response = session.get(check_url, proxies={'http': proxy, 'https': proxy}, timeout=timeout,
                                   headers={'User-Agent': USER_AGENT}, allow_redirects=False)
            try:
                if response.status_code in (401, 403, 429, 503):
                    return 'proxy_check_refused'
                observed = response.text.strip()[:64] if response.status_code == 200 else ''
            finally:
                close = getattr(response, 'close', None)
                if close is not None:
                    close()
    except (requests.ConnectionError, requests.Timeout):
        return 'proxy_unavailable'
    except (requests.RequestException, ValueError):
        return 'proxy_invalid'
    finally:
        if own:
            session.close()
    try:
        return '' if ipaddress.ip_address(observed) == expected else 'proxy_exit_mismatch'
    except ValueError:
        return 'proxy_exit_mismatch'


def new_heal_status():
    """Fresh per-invocation diagnostics; no command or proxy details."""
    return {'attempted': False, 'heal_exit': None, 'retries': 0, 'recovered': False}


def preflight_with_retry(proxy, *, environ=None, clock=None, host_dir=None):
    """Probe the same proxy with bounded waits, optionally attempting one heal.

    Hook success never authorizes fetching: only a successful probe does.
    Hook arguments, output and exception details must never reach diagnostics.
    """
    env = environ if environ is not None else os.environ
    clock = clock or Clock()
    status = new_heal_status()
    options = {'host_dir': host_dir} if host_dir is not None else {}
    reason = preflight(proxy, environ=env, **options)
    if not reason or reason in ('proxy_invalid', 'proxy_check_refused'):
        return reason, status
    command = env.get(HEAL_ENV, '').strip()
    if command:
        status['attempted'] = True
        try:
            args = shlex.split(command)
            if args:
                completed = subprocess.run(
                    args, shell=False, timeout=30, check=False,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                status['heal_exit'] = completed.returncode
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass  # Keep failures quiet and require a fresh successful probe.
    for wait in ((15, 30, 45) if command else (2, 4)):
        clock.sleep(wait)
        status['retries'] += 1
        reason = preflight(proxy, environ=env, **options)
        if not reason:
            status['recovered'] = True
            break
        if reason in ('proxy_invalid', 'proxy_check_refused'):
            break
    return reason, status
