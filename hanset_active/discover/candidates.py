"""Internal identity, provenance and collect-target promotion for continuous seeds."""
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import json
import math
import re
import unicodedata
from urllib.parse import parse_qsl, unquote, urlsplit

from ..collect.config import official_site
from ..collect.targets import canonical_requested_url
from ..db import deterministic_id
from ..util.pii import strip_pii
from ..util.timeutil import kst_now
from ..util.urls import canonicalize, is_collect_denied, literal_private, registrable_domain
from ..venues import is_article_title, normalize_name

TRANSITIONS = ('new', 'resolved', 'duplicate', 'rejected', 'promoted')
_ADDRESS_ALIASES = {
    '서울특별시': '서울', '부산광역시': '부산', '대구광역시': '대구',
    '인천광역시': '인천', '광주광역시': '광주', '대전광역시': '대전',
    '울산광역시': '울산', '세종특별자치시': '세종', '경기도': '경기',
    '강원특별자치도': '강원', '강원도': '강원', '충청북도': '충북', '충청남도': '충남',
    '전북특별자치도': '전북', '전라북도': '전북', '전라남도': '전남',
    '경상북도': '경북', '경상남도': '경남', '제주특별자치도': '제주', '제주도': '제주',
}
_SECRET_PARAMS = {
    'servicekey', 'service_key', 'api_key', 'apikey', 'access_token', 'token', 'key', 'auth',
    'authorization', 'session', 'sid', 'password', 'passwd', 'secret', 'sig', 'signature', 'cookie',
}


def clean_text(value, limit=180):
    if not isinstance(value, str):
        return ''
    value = ' '.join(unicodedata.normalize('NFKC', value).split())
    sanitized = strip_pii(value)
    if '[' in sanitized and re.search(r'\[(?:email|phone|token|redacted)\]', sanitized):
        return ''
    if '@' in value or len(value) > limit or any(not char.isprintable() for char in value):
        return ''
    return value


def address_key(value):
    text = unicodedata.normalize('NFKC', value or '').casefold()
    for full, short in _ADDRESS_ALIASES.items():
        text = text.replace(full, short)
    # Preserve hyphens and digit boundaries in road/lot/building numbers.
    text = re.sub(r'(?<=\d)\s*-\s*(?=\d)', '-', text)
    return re.sub(r'(?<!\d) +| +(?!\d)|[,()]', '', ' '.join(text.split()))


def name_key(value):
    return normalize_name(unicodedata.normalize('NFKC', value or ''))


def public_url(url, *, testing=False):
    """Accept anonymous, credential-free source URLs including HTTP provenance."""
    if not isinstance(url, str) or not url or len(url) > 500:
        return ''
    try:
        decoded = unquote(url)
        parsed = urlsplit(url)
        if (re.search(r'[\\\x00-\x20\x7f]', url) or re.search(r'[\\\x00-\x1f\x7f]', decoded)
                or strip_pii(decoded) != decoded or parsed.username is not None
                or parsed.password is not None or is_collect_denied(url)
                or any(k.casefold() in _SECRET_PARAMS for k, _ in parse_qsl(urlsplit(decoded).query))
                or re.search(r'\[(?:email|phone|token|redacted)\]', decoded, re.I)):
            return ''
        loopback = parsed.hostname in ('127.0.0.1', '::1', 'localhost')
        if literal_private(url) and not (testing and loopback):
            return ''
        if parsed.port not in (None, 80, 443) and not (testing and loopback):
            return ''
        canonical = canonicalize(url, preserve_path=True)
        if not re.fullmatch(r'[a-z0-9.:-]+', urlsplit(canonical).hostname):
            return ''
        return canonical
    except (TypeError, ValueError, UnicodeError):
        return ''


def target_url(url, *, testing=False):
    if safe := public_url(url, testing=testing):
        try:
            canonical_requested_url(safe, testing=testing)
            return safe
        except ValueError:
            pass
    return ''


def license_fields(fields):
    """Retain only typed license metadata from the adapter's projection."""
    result = {key: clean_text(fields.get(key)) for key in ('road_address', 'lot_address')}
    if fields.get('operating_status') == '01':
        result['operating_status'] = '01'
    stamp = fields.get('license_date')
    if isinstance(stamp, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', stamp):
        result['license_date'] = stamp
    coordinates = fields.get('coordinates')
    if isinstance(coordinates, dict) and coordinates.get('crs') == 'EPSG:5174':
        x, y = coordinates.get('x'), coordinates.get('y')
        if all(type(number) in (int, float) and math.isfinite(number) for number in (x, y)):
            result['coordinates'] = {'x': x, 'y': y, 'crs': 'EPSG:5174'}
    return result


@dataclass(frozen=True)
class Observation:
    name: str
    category: str
    source_kind: str
    source_url: str
    address: str = ''
    sido: str = ''
    sigungu: str = ''
    fetch_url: str = ''
    # Set only after a first-party page has been fetched and identity verified.
    ownership: str = ''
    single_venue: bool = False
    record_key: str = ''
    fields: dict = field(default_factory=dict)
    candidate_id: str = ''
    matched_identity: tuple[str, str] | None = None


class Candidates:
    def __init__(self, db, clock, *, profile, testing=False):
        self.db, self.clock, self.profile, self.testing = db, clock, profile, testing
        self.names, self.addresses, self.domains = (defaultdict(set) for _ in range(3))
        self.identities = {}
        self.public_identities = set()
        aliases = defaultdict(set)
        for row in db.execute('SELECT venue_id, normalized_name FROM venue_aliases'):
            aliases[row['venue_id']].add(row['normalized_name'])
        addresses = defaultdict(set)
        for row in db.execute("""SELECT venue_id, value_text FROM venue_fields
                                 WHERE field IN ('address','address_road','road_address','lot_address')
                                   AND valid_to IS NULL"""):
            if key := address_key(row['value_text']):
                addresses[row['venue_id']].add(key)
        for row in db.execute('SELECT * FROM venues'):
            vid = row['venue_id']
            domains = json.loads(row['domains'] or '[]')
            if not isinstance(domains, list):
                domains = []
            if row['website']:
                domains.append(row['website'])
                if target_url(row['website'], testing=testing):
                    self.public_identities.add(('venue', vid))
            self._index(('venue', vid), {name_key(row['name']), name_key(row['name_ko']), *aliases[vid]},
                        addresses[vid], self._owned_domains(domains))
        for row in db.execute("""SELECT * FROM seed_venues WHERE seed_id =
                              (SELECT cursor FROM run_checkpoints WHERE checkpoint_id='seed_freeze')"""):
            domains = self._owned_domains([row['website'] or row['domain']])
            # Prefer an existing venue for a frozen row with the same identity.
            matches = self.names[row['normalized_name']]
            if any(domains & self.identities[key][2] for key in matches if key[0] == 'venue'):
                continue
            key = ('frozen', row['seed_venue_id'])
            self._index(key, {row['normalized_name']}, set(), domains)
            if target_url(row['website'], testing=testing):
                self.public_identities.add(key)
        for row in db.execute("SELECT * FROM seed_candidates WHERE status!='rejected'"):
            self._index_candidate(row)
        # Preserve verified alternate names and public presence on restart.
        for row in db.execute("""SELECT s.source_kind, s.fields_json, s.fetch_url, s.official, s.ownership_evidence,
                                        c.id, c.matched_kind, c.matched_id
                                 FROM seed_candidate_sources s JOIN seed_candidates c ON c.id=s.candidate_id
                                 WHERE c.status!='rejected'"""):
            key = (row['matched_kind'] or 'candidate', row['matched_id'] or row['id'])
            if row['fetch_url']:
                self.public_identities.add(key)
            fields = json.loads(row['fields_json'])
            if row['source_kind'] == 'licenses':
                self._index(key, set(), {address_key(fields.get(column)) for column in
                                       ('road_address', 'lot_address')}, set())
            if row['official'] or row['ownership_evidence'] == 'matched_identity':
                self._index(key, {name_key(fields.get('name'))}, set(), set())

    def _owned_domains(self, values):
        result = set()
        for value in values:
            if not isinstance(value, str) or not value:
                continue
            url = value if '://' in value else 'https://' + value
            if public_url(url, testing=self.testing) and official_site(url, profile=self.profile):
                result.add(registrable_domain(url))
        return result

    def _index(self, key, names, addresses, domains, single=False):
        values = (names - {''}, addresses - {''}, domains - {''}, single)
        if key in self.identities:
            values = tuple(old | new for old, new in zip(self.identities[key], values))
        self.identities[key] = values
        for index, terms in zip((self.names, self.addresses, self.domains), values):
            for term in terms:
                index[term].add(key)

    def _index_candidate(self, row):
        self._index((row['matched_kind'] or 'candidate', row['matched_id'] or row['id']),
                    {row['normalized_name']}, {row['normalized_address']},
                    {row['official_domain'] or ''}, bool(row['single_venue']))

    def canonical_identity(self, candidate_id):
        row = self.db.execute('SELECT matched_kind, matched_id FROM seed_candidates WHERE id=?',
                              (candidate_id,)).fetchone()
        return (row['matched_kind'], row['matched_id']) if row and row['matched_kind'] else ('candidate', candidate_id)

    def _match(self, name, address, domain, single):
        names = self.names.get(name, set())
        addresses = self.addresses.get(address, set())
        domains = self.domains.get(domain, set())
        # Attribute a verified single-venue page by unique name when there is
        # neither conflicting branch address nor another owner of the domain.
        if domain and single and len(names) == 1:
            key = next(iter(names))
            known_addresses = self.identities[key][1]
            if not (domains - {key} or (address and known_addresses and key not in addresses)):
                domains = domains | {key}
        matches = (names & addresses) | (names & domains) | (addresses & domains)
        if address:
            matches = {key for key in matches if not self.identities[key][1] or key in addresses}
        if not matches and single and len(domains) == 1:
            key = next(iter(domains))
            known = self.identities[key]
            if known[3] and (not address or not known[1] or key in addresses):
                matches = domains
        return (next(iter(matches)) if len(matches) == 1 else None,
                len(matches) > 1 or (not matches and domain and
                    (len(names) > 1 or (names and not single and not address and not domains))))

    def known_public_presence(self, name, address):
        matches = self.names.get(name_key(name), set()) & self.addresses.get(address_key(address), set())
        return len(matches) == 1 and next(iter(matches)) in self.public_identities

    def record(self, observation):
        """Write inside the caller's transaction and count genuine transitions."""
        item = observation
        counts = Counter()
        name, address = clean_text(item.name), clean_text(item.address)
        normalized, addr = name_key(name), address_key(address)
        source = public_url(item.source_url, testing=self.testing)
        if not normalized or not source:
            return counts
        role_url = target_url(item.fetch_url, testing=self.testing)
        rejected = is_article_title(name)
        official = bool(role_url and item.ownership and official_site(role_url, profile=self.profile))
        domain = registrable_domain(role_url) if official else None
        key = item.record_key or deterministic_id(
            item.source_kind, source, public_url(item.fetch_url, testing=self.testing),
            item.candidate_id, item.matched_identity)
        fields = license_fields(item.fields) if item.source_kind == 'licenses' else {}
        fields.update(name=name, address=address,
                      sido=clean_text(item.sido, 40), sigungu=clean_text(item.sigungu, 60))
        revision = deterministic_id(fields, item.category, source, role_url, official, item.ownership, item.single_venue)
        prior = self.db.execute('SELECT * FROM seed_candidate_sources WHERE record_key=?', (key,)).fetchone()
        if prior and prior['revision'] == revision:
            return counts

        match, ambiguous = None, False
        if not rejected:
            match, ambiguous = self._match(normalized, addr, domain, item.single_venue and official)
            if item.candidate_id or item.matched_identity:
                expected = item.matched_identity or self.canonical_identity(item.candidate_id)
                if expected in self.identities and (match is None or match == expected) and not ambiguous:
                    match = expected
        if ambiguous:
            role_url, official, domain = '', False, None

        matched_kind = matched_id = None
        if prior:
            cid = prior['candidate_id']
        elif match and match[0] == 'candidate':
            cid = match[1]
        elif match:
            matched_kind, matched_id = match
            cid = deterministic_id('matched-seed', *match)
        else:
            cid = deterministic_id('seed-candidate', key)
        row = self.db.execute('SELECT * FROM seed_candidates WHERE id=?', (cid,)).fetchone()
        now = kst_now(self.clock).isoformat()
        if row is None:
            status = 'rejected' if rejected else 'duplicate' if matched_kind else 'new'
            counts[status] += 1
            self.db.execute('''INSERT INTO seed_candidates VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (cid, name, item.category, address, fields['sido'], fields['sigungu'], item.source_kind,
                 source, domain, now, now, status, 'article_title' if rejected else None, normalized,
                 addr, int(bool(official and item.single_venue)), matched_kind, matched_id))
        else:
            status = row['status']
            if not prior and match:
                counts['duplicate'] += 1
            if status not in ('rejected', 'duplicate') and match and match[0] != 'candidate':
                self.db.execute("UPDATE seed_candidates SET status='duplicate', matched_kind=?, matched_id=? WHERE id=?",
                                (*match, cid))
                status = 'duplicate'
                counts[status] += 1
            # License observations are the authority for this address projection.
            if address and (not row['address'] or item.source_kind == 'licenses'):
                self.db.execute('''UPDATE seed_candidates SET address=?, normalized_address=?,
                                   sido=coalesce(nullif(?,''),sido), sigungu=coalesce(nullif(?,''),sigungu)
                                   WHERE id=?''', (address, addr, fields['sido'], fields['sigungu'], cid))
            self.db.execute('UPDATE seed_candidates SET last_seen=? WHERE id=?', (now, cid))
        if rejected or status == 'rejected':
            role_url, official, domain = '', False, None
        self.db.execute('''INSERT INTO seed_candidate_sources VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(record_key) DO UPDATE SET source_url=excluded.source_url, fetch_url=excluded.fetch_url,
            official=excluded.official, ownership_evidence=excluded.ownership_evidence,
            label_status=excluded.label_status, fields_json=excluded.fields_json, revision=excluded.revision''',
            (key, cid, item.source_kind, source, role_url or None, int(official),
             (item.ownership if official else 'matched_identity' if match else None),
             'unverified' if official else 'source_claim', '미확인',
             json.dumps(fields, ensure_ascii=False, sort_keys=True), revision))
        if domain:
            self.db.execute('''UPDATE seed_candidates SET official_domain=coalesce(official_domain,?),
                               single_venue=max(single_venue,?) WHERE id=?''',
                            (domain, int(item.single_venue), cid))
        elif prior and prior['official']:
            self.db.execute('''UPDATE seed_candidates SET official_domain=NULL, single_venue=0
                               WHERE id=? AND NOT EXISTS (SELECT 1 FROM seed_candidate_sources
                               WHERE candidate_id=? AND official=1)''', (cid, cid))
        if role_url:
            self.public_identities.add(self.canonical_identity(cid))
        if role_url and status in ('new', 'resolved'):
            counts['resolved'] += status == 'new'
            counts['promoted'] += 1
            self.db.execute("UPDATE seed_candidates SET status='promoted' WHERE id=?", (cid,))
        elif status == 'promoted' and not self.db.execute(
                'SELECT 1 FROM seed_candidate_sources WHERE candidate_id=? AND fetch_url IS NOT NULL', (cid,)).fetchone():
            self.db.execute("UPDATE seed_candidates SET status='new' WHERE id=?", (cid,))
        updated = self.db.execute('SELECT * FROM seed_candidates WHERE id=?', (cid,)).fetchone()
        if updated['status'] != 'rejected':
            self._index_candidate(updated)
            extra_addresses = {address_key(fields.get(column)) for column in ('road_address', 'lot_address')}
            self._index(self.canonical_identity(cid), {normalized} if official else set(),
                        extra_addresses, {domain} if domain else set(), official and item.single_venue)
        return counts


def promoted_rows(db):
    """Read the promoted-source projection from the authorization snapshot."""
    return db.execute('''SELECT c.*, s.fetch_url, s.official, v.venue_key, v.state AS venue_state,
                         f.name AS frozen_name, f.website AS frozen_website, f.domain AS frozen_domain,
                         f.seed_venue_id AS current_frozen_id
                         FROM seed_candidates c JOIN seed_candidate_sources s ON s.candidate_id=c.id
                         LEFT JOIN venues v ON c.matched_kind='venue' AND v.venue_id=c.matched_id
                         LEFT JOIN seed_venues f ON c.matched_kind='frozen' AND f.seed_venue_id=c.matched_id
                           AND f.seed_id=(SELECT cursor FROM run_checkpoints WHERE checkpoint_id='seed_freeze')
                         WHERE c.status IN ('promoted','duplicate') AND s.fetch_url IS NOT NULL
                         ORDER BY c.id,s.record_key''').fetchall()
