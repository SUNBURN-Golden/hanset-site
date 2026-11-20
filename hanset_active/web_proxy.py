"""Optional egress proxy for daily web collection.

`HANSET_WEB_PROXY` (socks5h:// or socks5:// on a loopback host) is read by
the collect-web run alone. A transport outage allows subsequent jobs to use
direct egress. An unexpected exit IP stops the run. Model API calls use their
own direct connection.
"""
import ipaddress
import os
import shlex
import socket
import subprocess
from urllib.parse import urlsplit

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


class ProxyUnavailable(Exception):
    """The configured proxy failed on a fetch without public scope."""


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


def preflight(proxy, *, environ=None, session=None, timeout=10.0):
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
        response = session.get(check_url, proxies={'http': proxy, 'https': proxy}, timeout=timeout,
                               headers={'User-Agent': USER_AGENT}, allow_redirects=False)
        observed = response.text.strip()[:64] if response.status_code == 200 else ''
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


def preflight_with_retry(proxy, *, environ=None, clock=None):
    """Probe the same proxy with bounded waits, optionally attempting one heal.

    Hook success never authorizes fetching: only a successful probe does.
    Hook arguments, output and exception details must never reach diagnostics.
    """
    env = environ if environ is not None else os.environ
    clock = clock or Clock()
    status = new_heal_status()
    reason = preflight(proxy, environ=env)
    if not reason or reason == 'proxy_invalid':
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
        reason = preflight(proxy, environ=env)
        if not reason:
            status['recovered'] = True
            break
        if reason == 'proxy_invalid':
            break
    return reason, status
