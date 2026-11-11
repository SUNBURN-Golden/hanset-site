"""Optional egress proxy for the daily official-web collection only.

`HANSET_WEB_PROXY` (socks5h:// or socks5:// on a loopback host) is read by
the collect-web run alone. When it is set the run fails closed: an unreachable
proxy, or an exit IP other than `HANSET_WEB_PROXY_EXIT_IP` (when given), skips
every web fetch. There is no fallback to a direct connection. Model API calls
never use this proxy.
"""
import ipaddress
import os
import socket
from urllib.parse import urlsplit

ENV = 'HANSET_WEB_PROXY'
EXIT_ENV = 'HANSET_WEB_PROXY_EXIT_IP'
CHECK_ENV = 'HANSET_WEB_PROXY_CHECK_URL'
DEFAULT_CHECK_URL = 'https://api.ipify.org'
# Heavy resources never pass through the metered proxy.
HEAVY_TYPES = ('image/', 'font/', 'audio/', 'video/', 'application/font', 'application/x-font')
HEAVY_RESOURCES = frozenset({'image', 'font', 'media', 'imageset', 'texttrack', 'manifest'})


class ProxyUnavailable(Exception):
    """The configured proxy cannot carry traffic; the run must stop."""


def parse(value):
    """Return (scheme, host, port) for a loopback SOCKS5 URL, else raise ValueError."""
    parts = urlsplit(value or '')
    if parts.scheme not in ('socks5', 'socks5h') or not parts.hostname or parts.username or parts.password:
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
    scheme, host, port = parse(value)
    # Remote DNS keeps lookups on the proxy side too.
    return f'socks5h://{host}:{port}'


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
    if not reachable(proxy):
        return 'proxy_unavailable'
    expected = env.get(EXIT_ENV, '').strip()
    if not expected:
        return ''
    import requests
    own = session is None
    session = session or requests.Session()
    session.trust_env = False
    try:
        response = session.get(env.get(CHECK_ENV, '').strip() or DEFAULT_CHECK_URL,
                               proxies={'http': proxy, 'https': proxy}, timeout=timeout)
        observed = response.text.strip()[:64] if response.status_code == 200 else ''
    except (requests.RequestException, ValueError):
        return 'proxy_unavailable'
    finally:
        if own:
            session.close()
    return '' if observed == expected else 'proxy_exit_mismatch'
