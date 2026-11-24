"""Public directory, sitemap and page identity adapters."""
from collections import Counter, deque
from dataclasses import replace
import json
import re
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup
from lxml.etree import XMLSyntaxError

from . import checkpoints
from .candidates import Observation, address_key, clean_text, name_key, public_url
from .mentions import names as mentioned_names
from ..db import deterministic_id
from .sitemaps import advertised_sitemaps
from ..collect.config import official_site
from ..extract import sitemap
from ..util.urls import registrable_domain
from ..venues import is_article_title, venue_relevant

_TOPICS = re.compile(r'사우나|스파|찜질|목욕|온천|크로스핏|하이록스|피트니스|헬스|웰니스|'
                     r'\b(?:sauna|spa|bathhouse|bath|jjimjilbang|wellness|crossfit|hyrox|fitness|gym)\b', re.I)
_LOCAL_TYPES = {'LocalBusiness', 'SportsActivityLocation', 'ExerciseGym', 'HealthClub',
                'DaySpa', 'HealthAndBeautyBusiness', 'Hotel', 'LodgingBusiness', 'SportsEvent', 'Event'}
_SHARED_TYPES = {'Hotel', 'LodgingBusiness', 'HotelChain', 'Brand'}
_GENERIC_TITLES = {'spa', 'sauna', 'fitness', 'wellness', 'gym', 'home', 'homepage',
                   '사우나', '스파', '피트니스', '찜질방', '홈', '홈페이지'}
_REGION = re.compile(r'대한민국|한국|서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주|'
                     r'\b(?:Korea|KOR|KR|Seoul|Busan|Daegu|Incheon|Gwangju|Daejeon|Ulsan|Sejong|'
                     r'Gyeonggi|Gangwon|Chungcheong|Jeolla|Jeonbuk|Jeonnam|Gyeongsang|Jeju|Suwon|Goyang)\b', re.I)
_THIRD_PARTY_PATH = re.compile(r'(^|/)(?:blog|reviews?|news|posts?|articles?|directory|listings?|ota|booking)(/|$)', re.I)

_DIRECTORY_DETAIL = re.compile(r'/(?:affiliates?|gyms?|find-a-gym|events?|races?|boxes)/[^/?#]+', re.I)


def category_for(text, default='sauna'):
    for pattern, category in (
        (r'\bevents?\b|이벤트|행사|대회|\brace\b|\bfestival\b', 'wellness_event'),
        (r'crossfit|크로스핏', 'crossfit_box'), (r'hyrox|하이록스', 'hyrox_box'),
        (r'hotel|호텔', 'hotel_spa'), (r'부티크|boutique', 'boutique_sauna'),
        (r'찜질|jjimjilbang', 'jjimjilbang'), (r'fitness|gym|피트니스|헬스', 'gym'),
        (r'스파|\bspa\b', 'spa'),
    ):
        if re.search(pattern, text, re.I):
            return category
    return default


def structured_objects(values):
    stack = list(reversed(values))
    while stack:
        obj = stack.pop()
        if isinstance(obj, list):
            stack.extend(reversed(obj))
        elif isinstance(obj, dict):
            yield obj
            for key in ('@graph', 'mainEntity', 'itemListElement', 'item', 'location', 'department'):
                child = obj.get(key)
                if isinstance(child, (dict, list)):
                    stack.append(child)


def types_of(obj):
    declared = obj.get('@type', [])
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return {item for item in declared if isinstance(item, str)}
    return set()


def structured_address(value):
    if isinstance(value, dict):
        return ' '.join(clean_text(value.get(key)) for key in (
            'addressRegion', 'addressLocality', 'streetAddress', 'extendedAddress') if clean_text(value.get(key)))
    return clean_text(value)


def page_identity(content, url):
    soup = BeautifulSoup(content.decode('utf-8', errors='replace') if isinstance(content, bytes) else content, 'lxml')
    objects = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            objects.extend(structured_objects([json.loads(node.string or node.get_text())]))
        except (ValueError, TypeError, RecursionError):
            continue
    businesses = [obj for obj in objects if types_of(obj) & _LOCAL_TYPES and clean_text(obj.get('name'))]
    headings = [clean_text(h.get_text(' ', strip=True)) for h in soup.select('h1')]
    headline = next((h for h in headings if h), '')
    title = clean_text(soup.title.get_text(' ', strip=True)) if soup.title else ''
    headline = headline or title
    article = (any(types_of(obj) & {'Article', 'BlogPosting', 'NewsArticle', 'ItemList'} for obj in objects)
               or is_article_title(headline) or is_article_title(title)
               or bool(_THIRD_PARTY_PATH.search(urlsplit(url).path)))
    primary = next((obj for obj in businesses if name_key(obj['name']) == name_key(headline)),
                   businesses[0] if len(businesses) == 1 else {})
    name = clean_text(primary.get('name')) or re.split(r'\s*[|｜]\s*|\s+[-–]\s+', headline)[0].strip()
    if not name or name.casefold() in _GENERIC_TITLES:
        name = ''
    address = structured_address(primary.get('address'))
    if not address:
        node = soup.select_one('address, [itemprop="streetAddress"]')
        address = clean_text(node.get_text(' ', strip=True)) if node else ''
    # An organization claiming a URL is useful only on its own host.
    claimed = public_url(primary.get('url'))
    country = primary.get('address')
    if isinstance(country, dict):
        country = country.get('addressCountry')
    if isinstance(country, dict):
        country = country.get('name') or country.get('identifier')
    country = clean_text(country, 60)
    offsite_claim = claimed and registrable_domain(claimed) != registrable_domain(url)
    shared = (len({name_key(o['name']) for o in businesses}) > 1 or any(types_of(o) & _SHARED_TYPES for o in objects)
              or bool(re.search(r'/hotels?/[^/]+/|호텔', unquote(urlsplit(url).path), re.I)))
    text = soup.get_text(' ', strip=True)
    return dict(name=name, address=address, article=article, title=headline, shared=shared,
                venue_types=types_of(primary), country=country, claimed=claimed,
                structured=bool(primary), offsite_claim=bool(offsite_claim), text=text, objects=businesses)


class WebSeeds:
    def __init__(self, store, fetcher, *, heartbeat):
        self.store, self.fetcher = store, fetcher
        self.db, self.clock, self.profile = store.db, store.clock, store.profile
        self.cache = {}
        self.failed_sources = set()
        self.heartbeat = heartbeat
        self.directory_domains = {registrable_domain(row['url']) for row in self.profile['directories']}
        self.directory_domains.add('hyrox.com')

    def getter(self, source):
        def get(url, **options):
            self.heartbeat()
            # Credentialed API requests use this very same web policy.
            allowed = options.pop('url_allowed', None)
            fetched = self.fetcher.fetch(url, audit_source='seeds:' + source,
                                         url_allowed=lambda u: bool(public_url(u, testing=self.store.testing))
                                         and (allowed is None or allowed(u)), **options)
            if fetched.outcome == 'error' and fetched.status not in (404, 410):
                self.failed_sources.add(source)
            return fetched
        return get

    def page(self, url, source):
        safe = public_url(url, testing=self.store.testing)
        if safe not in self.cache:
            domain = registrable_domain(safe) if safe else None
            fetched = self.getter(source)(safe, url_allowed=lambda u: registrable_domain(u) == domain) if safe else None
            page = None
            if fetched and fetched.ok and 'html' in fetched.content_type:
                info = page_identity(fetched.content, fetched.url)
                if domain in self.directory_domains and source == 'directories':
                    from .directories import detail
                    details = detail(fetched.content, fetched.url, self.profile, public_source=True)
                    if details:
                        info['directory_detail'] = {'website': details['website']}
                        info['address'] = info['address'] or details['address']
                page = (fetched.url, info)
            self.cache[safe] = page
            # Cache only in this invocation. Keep memory bounded during full passes.
            if len(self.cache) > 256:
                del self.cache[next(iter(self.cache))]
        return self.cache[safe]

    def _ownership(self, url, info, *, via, address_matched=False):
        domain = registrable_domain(url)
        if (info['article'] or info['offsite_claim'] or not info['name'] or domain in self.directory_domains
                or not official_site(url, profile=self.profile)):
            return ''
        if address_matched:
            return 'venue_name_and_address'
        if info['structured'] and info['claimed'] and not info['offsite_claim']:
            return 'first_party_business'
        # An unstructured search hit or arbitrary external link by itself is a
        # source claim. Known-domain sitemap pages carry prior ownership context.
        if via == 'sitemaps':
            return 'venue_page_on_known_site'
        # For verified homepage destinations, require a venue-identifying title
        # and a host bearing that identity. Avoid self-authorizing generic blogs.
        name = name_key(info['name'])
        host_labels = (urlsplit(url).hostname or '').split('.')[:-1]
        if any(len(key) >= 4 and key in name for key in map(name_key, host_labels)):
            return 'venue_name_and_host'
        return ''

    def resolve(self, observation, url, *, via=None):
        response = self.page(url, via or observation.source_kind)
        if not response:
            return observation
        final, info = response
        wanted = name_key(observation.name)
        actual = name_key(info['name'])
        address_matched = bool(observation.address and info['address'] and
                               address_key(observation.address) == address_key(info['address']))
        if observation.address and info['address'] and not address_matched:
            return observation
        # A known venue can use a third-party article that actually names it.
        exact = wanted == actual
        # Exact known identity or an article containing that known identity.
        if not exact and not (info['article'] and wanted and wanted in name_key(info['text'])):
            return observation
        ownership = self._ownership(final, info, via=via or observation.source_kind,
                                    address_matched=address_matched) if exact else ''
        return replace(observation, fetch_url=final, address=observation.address or info['address'],
                       ownership=ownership, single_venue=bool(ownership and not info['shared']))

    def record_page(self, url, source, *, default_category='sauna', country_context=False, directory=None):
        response = self.page(url, source)
        if not response:
            return Counter()
        final, info = response
        identity = info['name']
        path = unquote(urlsplit(final).path)
        context = identity + ' ' + path
        if not country_context and not _REGION.search(' '.join((info['address'], info['country'], info['text'], path))):
            return Counter()
        # Match an independently known identity before rejecting an article title.
        if info['article']:
            names = mentioned_names({'text': info['text'], 'mentions': info['objects']}, public_source=True)
            possible = [name for name in names if name_key(name) in self.store.names]
            if len(possible) == 1:
                identity = clean_text(possible[0])
                entity = next((obj for obj in info['objects'] if clean_text(obj.get('name')) == identity), {})
                address = structured_address(entity.get('address'))
                with self.db:
                    matched = self.store.names[name_key(identity)]
                    if len(matched) == 1:
                        return self.store.record(Observation(identity, default_category, source, final,
                                                             address, fetch_url=final, matched_identity=next(iter(matched))))
            if is_article_title(info['title']) and (venue_relevant(context, context, self.profile) or _TOPICS.search(context)):
                with self.db:
                    return self.store.record(Observation(info['title'], default_category, source, final))
            return Counter()
        structured_topics = {'ExerciseGym': 'gym', 'HealthClub': 'gym', 'DaySpa': 'spa',
                             'SportsEvent': 'wellness_event'}
        structured_category = next((category for kind, category in structured_topics.items()
                                    if kind in info['venue_types']), '')
        recognized = (structured_category or name_key(identity) in self.store.names
                      or venue_relevant(identity, '', self.profile) or _TOPICS.search(identity))
        named_venue_page = info['structured'] and _TOPICS.search(path)
        if not identity or not (recognized or named_venue_page):
            return Counter()
        ownership = self._ownership(final, info, via=source)
        observation = Observation(identity, category_for(context, structured_category or default_category), source, final,
                                  info['address'], fetch_url=final, ownership=ownership,
                                  single_venue=bool(ownership and not info['shared']))
        with self.db:
            counts = self.store.record(observation)
        if directory:
            parsed = info.get('directory_detail')
            if parsed and parsed['website']:
                own = self.resolve(replace(observation, fetch_url='', ownership='', single_venue=False), parsed['website'])
                if own.fetch_url:
                    with self.db:
                        counts.update(self.store.record(own))
        return counts

    def directories(self):
        from .directories import public_listing
        counts, usable, partial = Counter(), False, False
        entries = [row for row in self.profile['directories'] if row.get('enabled', True)
                   and row.get('directory') and row['kind'] in ('html_list', 'sitemap')]
        entries.append({'id': 'hyrox_events', 'kind': 'html_list', 'url': 'https://hyrox.com/find-my-race/'})
        get = self.getter('directories')
        for row in entries:
            category = {'crossfit': 'crossfit_box', 'hyrox': 'hyrox_box', 'hyrox_events': 'wellness_event'}.get(row['id'], 'sauna')
            if row['kind'] == 'sitemap':
                found, status = self._sitemap_walk(row['url'], [row['url']], source='directories',
                                                    terms=row.get('slug_include', ()), directory=row)
                counts.update(found)
                usable |= status == 'ok'
                partial |= status.startswith('partial:')
                continue
            for listing_url, info in public_listing(get, row['url'], public_url):
                for obj in info['objects']:
                    location = obj.get('location')
                    location_address = location.get('address') if isinstance(location, dict) else None
                    raw_address = obj.get('address') or location_address
                    addr = structured_address(raw_address)
                    country = raw_address.get('addressCountry', '') if isinstance(raw_address, dict) else ''
                    if not _REGION.search(f"{addr} {country} {obj.get('name', '')}"):
                        continue
                    url = public_url(urljoin(listing_url, obj['url'])) if isinstance(obj.get('url'), str) else ''
                    name = clean_text(obj.get('name'))
                    if not name or name_key(name) in _GENERIC_TITLES:
                        continue
                    usable = True
                    item = Observation(name, category, 'directories', listing_url, addr,
                                       record_key=deterministic_id('directory-entry', listing_url, url, name_key(name), address_key(addr)))
                    if url:
                        item = self.resolve(item, url)
                    with self.db:
                        counts.update(self.store.record(item))
                for url, text in info['links']:
                    local = bool(_REGION.search(url + ' ' + text))
                    configured_detail = row.get('url_filter') and re.search(row['url_filter'], url)
                    on_directory = registrable_domain(url) == registrable_domain(listing_url)
                    if local or configured_detail or (on_directory and _DIRECTORY_DETAIL.search(urlsplit(url).path)):
                        usable = True
                        counts.update(self.record_page(url, 'directories', country_context=local,
                                                       default_category=category, directory=row))
        if partial or 'directories' in self.failed_sources:
            return counts, 'partial:directory_request'
        return counts, 'ok' if usable else 'skipped:no_public_listing'

    def sitemaps(self):
        origins = set()
        for row in self.db.execute('SELECT website FROM venues WHERE state IS NOT \'closed\' UNION '
                                   'SELECT website FROM seed_venues WHERE seed_id='
                                   "(SELECT cursor FROM run_checkpoints WHERE checkpoint_id='seed_freeze') UNION "
                                   'SELECT fetch_url FROM seed_candidate_sources WHERE official=1 UNION '
                                   "SELECT 'https://' || domain FROM seed_domains WHERE seed_id="
                                   "(SELECT cursor FROM run_checkpoints WHERE checkpoint_id='seed_freeze')"):
            url = public_url(row[0], testing=self.store.testing)
            if url and official_site(url, profile=self.profile):
                parts = urlsplit(url)
                origins.add(f'{parts.scheme}://{parts.netloc}')
        counts, statuses = Counter(), []
        origin_cursor = checkpoints.load(self.db, 'sitemaps', 'origin_rotation').get('origin', '')
        ordered = sorted(origins, key=lambda origin: (origin <= origin_cursor, origin))
        for origin in ordered:
            getter = self.getter('sitemaps')
            domain = registrable_domain(origin)
            roots = [origin + '/sitemap.xml']
            robots = (getter(origin + '/robots.txt', kind='robots',
                             url_allowed=lambda u: registrable_domain(u) == domain)
                      if self.fetcher.policy.robots(origin) else None)
            if robots and robots.ok:
                for item in advertised_sitemaps(robots.text):
                    if (safe := public_url(urljoin(origin, item), testing=self.store.testing)) and registrable_domain(safe) == domain:
                        roots.append(safe)
            found, status = self._sitemap_walk(origin, roots)
            counts.update(found)
            statuses.append(status)
            with self.db:
                checkpoints.save(self.db, 'sitemaps', 'origin_rotation', {'origin': origin}, self.clock)
        if 'sitemaps' in self.failed_sources or any(s.startswith('partial:') for s in statuses):
            return counts, 'partial:sitemap_request'
        return counts, 'ok' if statuses else 'skipped:no_known_sites'

    def _sitemap_walk(self, key, roots, *, source='sitemaps', terms=(), directory=None):
        domain = registrable_domain(roots[0])
        progress = checkpoints.load(self.db, source, key)
        fresh = [(url, 0) for url in dict.fromkeys([*roots, *progress.get('retry', ())])]
        queue = deque(progress.get('queue', fresh))
        visited = set(progress.get('completed', ()))
        retry, counts, any_map = [], Counter(), False
        while queue:
            url, cursor = queue.popleft()
            if url in visited:
                continue
            fetched = self.getter(source)(url, kind='sitemap', url_allowed=lambda u: registrable_domain(u) == domain)
            if not fetched.ok:
                # Missing /sitemap.xml is common when another root is advertised.
                if fetched.status not in (404, 410):
                    retry.append((url, cursor))
                continue
            try:
                doc = sitemap.extract(fetched.content, fetched.url)
            except (ValueError, TypeError, XMLSyntaxError):
                continue
            any_map = True
            links = [public_url(urljoin(fetched.url, child), testing=self.store.testing) for child in doc['urls']]
            links = [link for link in links if link and registrable_domain(link) == domain]
            if doc['kind'] == 'index':
                child_terms = directory.get('child_include', ()) if directory else ()
                queue.extend((child, 0) for child in links if child not in visited and
                             (not child_terms or any(re.search(t, unquote(child), re.I) for t in child_terms)))
            else:
                # Save the next offset after each committed group, so interruption
                # only replays an idempotent tail and traversal continues past it.
                for index, link in enumerate(links[cursor:], cursor + 1):
                    path = unquote(urlsplit(link).path)
                    if (any(re.search(t, path, re.I) for t in terms) if terms else _TOPICS.search(path)):
                        counts.update(self.record_page(link, source, country_context=True, directory=directory))
                    if index % 32 == 0:
                        with self.db:
                            checkpoints.save(self.db, source, key, {
                                'queue': [(url, index), *queue, *retry], 'completed': sorted(visited)}, self.clock)
            visited.add(url)
            with self.db:
                checkpoints.save(self.db, source, key, {
                    'queue': [*queue, *retry], 'completed': sorted(visited)}, self.clock)
        with self.db:
            checkpoints.save(self.db, source, key, {
                'retry': [url for url, _ in retry]} if retry else {}, self.clock)
        return counts, 'partial:sitemap_request' if retry else 'ok' if any_map else 'skipped:no_sitemap'
