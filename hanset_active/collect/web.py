"""Public-page classification and first-party information link selection."""
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from hanset_active.extract import html as html_rules
from hanset_active.util.urls import canonicalize, is_excluded

from .ground import redact
from .safety import POLICY_RE, asset_type, looks_like_security
from .config import SUBPAGE_MAX, official_site, scope_allowed
from .targets import LINK_IDENTITY, _https_or_test, canonical_requested_url

_KEYWORDS = {
    **dict.fromkeys(('요금', '가격', '이용요금', 'price', 'pricing', 'rates', '드롭인',
                     'drop-in', '이용시간', '운영시간', '영업시간', '시간표', 'schedule',
                     'sauna', '사우나', '찜질', '찜질방', 'jjimjilbang'), 3),
    **dict.fromkeys(('시설', 'facilities', 'spa', '이용가이드', 'guide', 'floor-guide', '층별'), 2),
    **dict.fromkeys(('안내', '이용안내', 'info', 'about', 'membership'), 1),
}
# In addition to asset_type's image/PDF checks, never queue downloadable files.
_DOWNLOAD_EXTENSIONS = ('.zip', '.gz', '.tar', '.rar', '.7z', '.exe', '.dmg',
                        '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.hwp',
                        '.hwpx', '.csv', '.mp3', '.wav', '.ogg', '.webm', '.mov')


def select_subpages(content, parent_url, domains, *, testing=False, limit=SUBPAGE_MAX, links=None):
    """Rank safe information links from guarded raw HTML, including navigation.

    Returns at most ``limit`` candidates, or every ranked candidate when None;
    the fetch queue enforces the per-target cap after cooldown filtering so a
    cooling candidate never consumes a slot.
    """
    from hanset_active.config import load_profile
    from hanset_active.util.urls import registrable_domain

    try:
        parent = canonical_requested_url(parent_url, testing=testing, identity_mode=LINK_IDENTITY)
    except (ValueError, TypeError, UnicodeError):
        return []
    parent_domain = registrable_domain(parent)
    # One profile read per page, not per anchor on link-heavy homepages.
    profile = load_profile()
    terms = profile['exclusion_terms']
    scores, fetch_urls = {}, {}
    if links is None:
        links = ((anchor['href'], anchor.get_text(' ', strip=True))
                 for anchor in BeautifulSoup(content, 'lxml').select('a[href]:not([download])'))
    for href, text in links:
        try:
            candidate = urljoin(parent_url, href)
            if not _https_or_test(candidate, testing=testing):
                continue
            # Validate the original identity before stripping trackers/fragments.
            url = canonical_requested_url(candidate, testing=testing, identity_mode=LINK_IDENTITY)
            path = unquote(urlsplit(url).path).casefold()
            if (url == parent or registrable_domain(url) != parent_domain
                    or not scope_allowed(url, domains) or not official_site(url, profile=profile)
                    or POLICY_RE.search(path) or asset_type('', path)
                    or path.endswith(_DOWNLOAD_EXTENSIONS)):
                continue
            label = ' '.join((text + ' ' + path).split()).casefold()
            if is_excluded(label, terms=terms):
                continue
            fetch_urls.setdefault(url, canonicalize(candidate, preserve_path=True))
            score = sum(weight for word, weight in _KEYWORDS.items() if word in label)
            if score:
                scores[url] = max(score, scores.get(url, 0))
        except (TypeError, ValueError, UnicodeError):
            continue
    return [fetch_urls[url] for url in sorted(scores, key=lambda url: (-scores[url], url))[:limit]]


def classify_page(fetched, scope_domains):
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
    if not scope_allowed(url, scope_domains):
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
    text = redact(document.get('text') or '')
    if looks_like_security(text):
        return 'blocked', None
    if len(text) < 80:
        return 'empty_body', None
    return 'ready', text
