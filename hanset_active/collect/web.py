"""Official-page classification after the existing guarded fetcher returns."""
from urllib.parse import urlsplit

from hanset_active.extract import html as html_rules
from hanset_active.util.urls import is_denied, is_excluded

from .ground import bounded_text
from .safety import POLICY_RE, asset_type, looks_like_security


def classify_page(fetched, domains):
    """Return a disposition and transient main text. Text is None unless the page is usable."""
    url = fetched.url or ''
    if fetched.outcome == 'cap_reached':
        return 'cap_reached', None
    if fetched.outcome == 'denied':
        return ('excluded' if fetched.reason == 'excluded' else 'denied'), None
    if fetched.outcome != 'ok':
        outcome = fetched.outcome if fetched.outcome in {
            'robots_disallowed', 'login_wall', 'blocked', 'empty_body', 'error'} else 'error'
        return outcome, None
    if asset_type(fetched.content_type, url):
        return 'asset_document', None
    if is_denied(url) or is_excluded(url) or registrable_miss(url, domains):
        return 'off_domain', None
    path = urlsplit(url).path
    if POLICY_RE.search(path):
        return 'policy_page', None
    try:
        document = html_rules.extract(fetched.content or b'', url)
    except (TypeError, ValueError):
        return 'error', None
    title = document.get('title') or ''
    if POLICY_RE.search(title):
        return 'policy_page', None
    text = bounded_text(document.get('text') or '')
    if looks_like_security(text):
        return 'blocked', None
    if is_excluded(text, title):
        return 'excluded', None
    if len(text) < 80:
        return 'empty_body', None
    return 'ready', text


def registrable_miss(url, domains):
    from hanset_active.util.urls import registrable_domain
    try:
        return registrable_domain(url) not in domains
    except (TypeError, ValueError):
        return True
