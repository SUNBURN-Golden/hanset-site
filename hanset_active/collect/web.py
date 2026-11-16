"""Official-page classification after the existing guarded fetcher returns."""
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from hanset_active.extract import html as html_rules
from hanset_active.util.urls import canonicalize, is_denied, is_excluded

from .ground import bounded_text
from .safety import POLICY_RE, asset_type, looks_like_security
from .config import SUBPAGE_MAX, domain_allowed
from .targets import _https_or_test, canonical_requested_url

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


def select_subpages(content, parent_url, domains, *, testing=False, limit=SUBPAGE_MAX):
    """Rank safe information links from guarded raw HTML, including navigation.

    Returns at most ``limit`` candidates, or every ranked candidate when None;
    the fetch queue enforces the per-target cap after cooldown filtering so a
    cooling candidate never consumes a slot.
    """
    from hanset_active.config import load_profile
    from hanset_active.util.followups import portal_domains
    from hanset_active.util.urls import registrable_domain

    try:
        parent = canonical_requested_url(parent_url, testing=testing)
    except (ValueError, TypeError, UnicodeError):
        # Redirect targets only pass canonicalize(), not the requested-URL
        # guard (e.g. a ?sid= landing page). The page itself fetched fine;
        # without a safe parent identity it simply supplies no children.
        return []
    parent_domain = registrable_domain(parent)
    # One profile read per page, not per anchor on link-heavy homepages.
    profile = load_profile()
    portals = portal_domains(profile)
    terms = profile['exclusion_terms']
    scores, fetch_urls = {}, {}
    soup = BeautifulSoup(content, 'lxml')
    for anchor in soup.select('a[href]'):
        if anchor.has_attr('download'):
            continue
        try:
            candidate = urljoin(parent_url, anchor['href'])
            if not _https_or_test(candidate, testing=testing):
                continue
            # Validate the original identity before stripping trackers/fragments.
            url = canonical_requested_url(candidate, testing=testing)
            path = unquote(urlsplit(url).path).casefold()
            if (url == parent or registrable_domain(url) != parent_domain
                    or registrable_domain(url) in portals or not domain_allowed(url, domains)
                    or POLICY_RE.search(path) or asset_type('', path)
                    or path.endswith(_DOWNLOAD_EXTENSIONS)):
                continue
            label = ' '.join((anchor.get_text(' ', strip=True) + ' ' + path).split()).casefold()
            if is_excluded(label, terms=terms):
                continue
            fetch_urls.setdefault(url, canonicalize(candidate, preserve_path=True))
            score = sum(weight for word, weight in _KEYWORDS.items() if word in label)
            if score:
                scores[url] = max(score, scores.get(url, 0))
        except (TypeError, ValueError, UnicodeError):
            continue
    return [fetch_urls[url] for url in sorted(scores, key=lambda url: (-scores[url], url))[:limit]]


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
