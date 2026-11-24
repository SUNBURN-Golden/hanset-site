"""Check active evidence using only the transient fetched document."""
import json
import re
from urllib.parse import urlsplit
from functools import lru_cache

from ..db import deterministic_id
from ..util.followups import portal_domains
from ..util.urls import registrable_domain
from . import fields as field_rules


@lru_cache(maxsize=1)
def portals():
    return frozenset(portal_domains())


_EVIDENCE_FIELDS = set(('foreigner_policy tattoo_policy bath_composition hours closures price_day_pass '
                        'cold_bath_temp_c sauna_temp_c rapid_cold_plunge min_age access_policy').split())

def _policy_page(url, title=''):
    from urllib.parse import unquote
    return bool(re.search(r'privacy|policy|terms|약관|개인정보', unquote(urlsplit(url).path) + ' ' + title, re.I))


def _roundup_section(doc, url, quote):
    """Return the local section (or None for a non-roundup document)."""
    text = doc['text']
    headings = doc['structured'].get('headings', [])
    starts = {0}
    for heading in headings:
        starts.update(m.start() for m in re.finditer(r'(?m)^' + re.escape(heading) + r'$', text))
    labels = list(re.finditer(r'(?im)^.*?(?:영업시간|운영시간|opening hours)\s*[:：]', text))
    # Portals also use paragraph labels rather than semantic HTML headings.
    preceding = []
    for label in labels:
        prefix = text[:label.start()].rstrip()
        line = prefix.split('\n')[-1] if prefix else ''
        if line:
            preceding.append((len(prefix) - len(line), line))
    roundup = bool(re.search(r'editors-?picks|/list|best', urlsplit(url).path, re.I)) or len({line for _, line in preceding}) >= 2
    if not roundup:
        return None
    if len(starts) == 1:
        starts.update(offset for offset, _ in preceding)
    boundaries = sorted(starts) + [len(text)]
    # Reject ambiguous repeated quotes unless every occurrence belongs to the same section.
    locations = [m.start() for m in re.finditer(re.escape(quote), text)] if quote else []
    sections = {(a, b) for at in locations for a, b in zip(boundaries, boundaries[1:]) if a <= at < b}
    if len(sections) != 1:
        return ''
    a, b = sections.pop()
    return text[max(a, locations[0] - 600):b]


ACTIVE_VALIDATION_VERSION = 1


def page_verdict(obs, doc, url, content_type, venue, *, portal_domains=None):
    portal_domains = portals() if portal_domains is None else portal_domains
    path = urlsplit(url).path.lower()
    ct = (content_type or '').lower()
    if any(t in ct for t in ('text/css', 'javascript', 'font', 'image')) or re.search(r'\.(css|js|mjs|woff2?|ttf|svg|png|jpe?g|gif|webp|ico)$', path):
        return 'asset_document'
    if obs['field'] in ('hours', 'closures', 'price_day_pass') and (any(t in ct for t in ('rss', 'atom', 'xml')) or re.search(r'\.(xml|rss|atom)$|feed', path)):
        return 'feed_post_not_current'
    if doc is None:
        return 'raw_unavailable'
    if obs['field'] == 'website':
        value = json.loads(obs['value_json']) if obs['value_json'] else ''
        if not isinstance(value, str) or _policy_page(value) or registrable_domain(value) in portal_domains or _policy_page(url, doc['title']):
            return 'not_venue_website'
    if obs['field'] == 'hours' and _policy_page(url, doc['title']):
        return 'policy_page'
    section = _roundup_section(doc, url, obs['quote'] or '')
    if section is not None:
        norm = lambda t: ''.join(c for c in (t or '').casefold() if c.isalnum())
        names = [norm(venue[k]) for k in ('name', 'name_ko') if venue is not None and norm(venue[k])]
        # Only preceding local context establishes ownership; later venues cannot lend their name.
        prefix = section.split(obs['quote'], 1)[0] if obs['quote'] else ''
        if not any(n in norm(prefix + (obs['quote'] or '')) for n in names):
            return 'roundup_other_section'
    if venue is not None:
        domains = {registrable_domain(d if '://' in d else 'https://' + d) for d in json.loads(venue['domains'] or '[]')}
        if venue['website']:
            domains.add(registrable_domain(venue['website']))
        domains -= portal_domains  # portal pages count only when their title names the venue
        norm = lambda t: ''.join(c for c in (t or '').casefold() if c.isalnum())
        names = [norm(venue[k]) for k in ('name', 'name_ko') if norm(venue[k])]
        if section is None and registrable_domain(url) not in domains and not any(n in norm(doc['title']) for n in names):
            return 'off_topic_page'
    quote = obs['quote'] or ''
    if obs['field'] in ('foreigner_policy', 'tattoo_policy') and field_rules.USER_REVIEW.search(quote):
        return 'user_review_text'
    if obs['field'] == 'hours' and field_rules.BREAK_CUE.search(quote):
        value = json.loads(obs['value_json']) if obs['value_json'] else {}
        for m in re.finditer(r'\b\d{1,2}:\d{2}\s*[~–—-]\s*\d{1,2}:\d{2}', quote):
            hours = '-'.join(f'{int(h):02d}:{minute}' for h, minute in re.findall(r'(\d{1,2}):(\d{2})', m[0]))
            if hours == value.get('hours') and field_rules.BREAK_CUE.search(field_rules.hours_prefix(quote, m.start())):
                return 'break_hours'
    if obs['field'] == 'hours' and re.search(r'maintenance|점검|정기\s*청소', obs['quote'] or '', re.I):
        return 'maintenance_window'
    if obs['field'] == 'hours' and field_rules.TRANSPORT_CUE.search(obs['quote'] or '') and \
            not any(r['field'] == 'hours' for r in field_rules.extract(obs['quote'] or '')):
        return 'transport_hours'
    if obs['field'] in _EVIDENCE_FIELDS:
        norm_quote = lambda t: re.sub(r'[\s|]+', '', t or '')
        tables = '\n'.join(' '.join(cells) for table in doc['structured']['tables'] for cells in table)
        quote = norm_quote(obs['quote'])
        if not quote or quote not in norm_quote(doc['text'] + '\n' + tables):
            return 'quote_not_in_main_text'
    if obs['field'] in ('bath_composition', 'hours', 'closures', 'foreigner_policy',
                        'tattoo_policy', 'rapid_cold_plunge', 'cold_bath_temp_c', 'sauna_temp_c'):
        expected_field = obs['field']
        value = json.loads(obs['value_json']) if obs['value_json'] else None
        if expected_field == 'hours' and isinstance(value, dict) and value.get('hours') == 'open_every_day':
            expected_field = 'closures'
        current = [row for row in field_rules.extract(obs['quote'] or '') if row['field'] == expected_field]
        if not current:
            return 'rule_no_longer_matches'
        # Scalar policies: an observation stored by an older rule version must still give the same value.
        if expected_field in ('tattoo_policy', 'foreigner_policy') and not any(row['value'] == value for row in current):
            return 'value_no_longer_matches'
    return None

def active_validation(item, doc, url, content_type, venue):
    """Validate against the in-memory document and keep hashes and the verdict."""
    obs = {**item, 'value_json': json.dumps(item['value'], ensure_ascii=False)}
    doc = {**doc, 'title': doc.get('title', ''), 'text': doc.get('text', ''),
           'structured': {**doc.get('structured', {}),
                          'tables': doc.get('structured', {}).get('tables', [])}}
    section = _roundup_section(doc, url, item['quote'])
    return {'version': ACTIVE_VALIDATION_VERSION,
            'reason': page_verdict(obs, doc, url, content_type, venue),
            'section': deterministic_id('active-section', section) if section is not None else None,
            'evidence_hash': deterministic_id(item['quote'])}
