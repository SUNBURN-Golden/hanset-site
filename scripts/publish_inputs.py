"""Operations projection from approved collector facts to static publication inputs."""

from copy import deepcopy
import csv
import json
import re
from collections import Counter, defaultdict
from io import StringIO

from hanset_active.collect import change_feeds, changes
from hanset_active.collect.targets import canonical_requested_url

import build_site as builder
import release_manifest as rm
from hanset_active.collect.normalize import normalize_value

PRICE_NOTE = re.compile(
    r'\bno\s+(?:\w+\s+){0,3}(?:amount|price|rate|fee)s?\b'
    r'|\b(?:amount|price|rate|fee)s?\b.{0,80}\b(?:pending|not (?:confirmed|listed|stated|published|disclosed)'
    r'|aren.t stated|isn.t stated|unconfirmed|unknown)\b', re.I,
)


def get_csv(root, name):
    with (root / name).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        return reader.fieldnames, list(reader)


def put_fact(record, en, key, ko_key, en_value, ko_value):
    for i, (label, _) in enumerate(en['facts']):
        if label == key:
            en['facts'][i][1] = en_value
            record['facts'][i][1] = ko_value
            return
    en['facts'].append([key, en_value])
    record['facts'].append([ko_key, ko_value])


def regular_slot(field, qualifiers, record, en):
    if qualifiers.get('scope', 'unspecified') not in ('unspecified', 'regular', 'general', 'venue', 'public'):
        return None
    if field == 'price_day_pass':
        box = record['csv'] == 'boxes'
        if set(qualifiers) - {'scope', 'ticket'} or qualifiers.get('ticket') != ('drop_in' if box else 'day_pass'):
            return None
        prices = [(key, term) for key, term in en['facts'] if '{price}' in term]
        labels = {'Price', 'Drop-in', 'Drop-in price', 'Drop-in, single'} if box else {'Price', 'Day pass'}
        headline_labels = labels | ({'Walk-in'} if box else {'Walk-in', 'Standard, per person'})
        if en.get('price_label', 'Price') not in headline_labels:
            return None
        # A collector's unqualified ticket cannot replace a compound or scoped
        # price whose other terms and venue restrictions it did not verify.
        if prices:
            if len(prices) != 1:
                return None
            key, term = prices[0]
            if key not in labels or sum(label == key for label, _ in en['facts']) != 1 or not re.fullmatch(
                r'(?:standard(?:, per person)? |walk-in |general )?\{price\}(?: \(tax incl\.\))?', term, re.I
            ):
                return None
            return key
        price_facts = [(key, term) for key, term in en['facts'] if builder.is_price_context(key)]
        if not price_facts:
            return 'Price'
        if len(price_facts) == 1:
            key, term = price_facts[0]
            if key in labels and builder.PENDING_CLAIM.search(term):
                return key
        return None
    if set(qualifiers) - {'scope'}:
        return None
    hours = [(key, term) for key, term in en['facts'] if builder.is_hours_key(key, term)
             and (key == 'Hours' or builder.claim_scope(key, term) != 'closure')]
    if not hours or (len(hours) == 1 and hours[0][0] == 'Hours' and (
        builder.PENDING_CLAIM.search(hours[0][1])
        or hours[0][1] == '24 hours'
        or re.fullmatch(r'\d{2}:\d{2}[-–—~]\d{2}:\d{2}', hours[0][1])
    )):
        return 'Hours'
    return None


def compatible_price_notes(en, key, amount):
    notes = [en.get('summary', ''), en.get('price_caveat', ''), *en.get('tips', []), *en.get('unverified', [])]
    notes.extend(term for label, term in en['facts'] if label != key and builder.is_price_context(label))
    for note in notes:
        if '{price}' in note or PRICE_NOTE.search(note):
            return False
        for match in builder.PRICE_AMOUNT.finditer(note):
            if re.search(r'[$€£%]|USD', match[0], re.I) or re.sub(r'[^\d]', '', match[0]) != str(amount):
                return False
    return True


def price_or_hours(item, key, record, en, row):
    field, value = item['field'], item['value']
    if field == 'price_day_pass':
        if not compatible_price_notes(en, key, value):
            return False
        put_fact(record, en, key, '요금', '{price}', '{price}')
        row['drop_in_price_krw' if record['csv'] == 'boxes' else 'price_krw'] = str(value)
        value = builder.won(str(value))
    else:
        value = '24 hours' if value == '24_hours' else value.replace('-', '–')
        put_fact(record, en, key, '운영시간', value, value)
    record.setdefault('price_hours_evidence', {})[key] = {
        'value': value, 'source_kind': 'official_page', 'source_url': item['requested_url'], 'checked_on': item['checked'],
    }
    return True


def event_scope(event):
    return tuple(event[key] for key in ('venue_id', 'venue_key', 'field', 'qualifier_key'))


def current_events(db, ids, today):
    """Read immutable transitions whose latest observation still supports them."""
    rows = {}
    conflicts, holds = changes.source_conflicts(db), changes.review_holds(db)
    for change_id in sorted(ids):
        row = db.execute("""
            SELECT event.*, observation.values_json AS supporting_values
            FROM web_change_events AS event
            JOIN web_change_baselines AS baseline USING
                (venue_id, venue_key, field, qualifier_key, source_identity, target_fingerprint)
            JOIN web_change_observations AS observation ON
                observation.observation_id = event.new_observation_id
                AND observation.processed = 1
                AND observation.venue_id = event.venue_id AND observation.venue_key = event.venue_key
                AND observation.source_identity = event.source_identity
                AND observation.target_fingerprint = event.target_fingerprint
                AND observation.requested_url = event.requested_url AND observation.final_url = event.final_url
                AND observation.day = event.checked
            WHERE event.change_id = ? AND baseline.state = 'active'
                AND baseline.last_event_id = event.change_id
                AND baseline.comparison_key = event.new_comparison_key
                AND event.event_kind = event.field AND COALESCE(event.review_reason, '') = ''
                AND event.rights = 'internal_review_only' AND event.auto_publish_to_site = 0
                AND NOT EXISTS (
                    SELECT 1 FROM web_change_events AS later
                    WHERE later.venue_id = event.venue_id AND later.venue_key = event.venue_key
                        AND later.field = event.field AND later.qualifier_key = event.qualifier_key
                        AND later.source_identity = event.source_identity
                        AND later.target_fingerprint = event.target_fingerprint
                        AND later.event_kind = 'qualifier_missing' AND later.event_seq > event.event_seq
                        AND later.observed_at >= baseline.observed_at
                )
            """, (change_id,)).fetchone()
        if row is None:
            continue
        event = dict(row)
        field = event['field']
        supporting = json.loads(event.pop('supporting_values'))
        if not any(item['field'] == field and item.get('qualifier_key') == event['qualifier_key']
                   and not item.get('reason') and item.get('value_json') == event['new_value_json']
                   for item in supporting):
            continue
        checked = rm.iso_date(event['checked'])
        if (field not in changes.FIELDS or event_scope(event) in conflicts or event_scope(event) in holds
                or checked is None or checked > today or not change_feeds._safe_event(event)):
            continue
        if not all(rm.web_host(event[key]) and canonical_requested_url(event[key]) == event[key]
                   for key in ('requested_url', 'final_url')):
            continue
        qualifiers, value = json.loads(event['qualifier_key']), json.loads(event['new_value_json'])
        if not isinstance(qualifiers, dict):
            continue
        if field == 'closures':
            raw = value
        else:
            raw = {**qualifiers, 'hours' if field == 'hours' else 'amount': value}
        norm = normalize_value(field, raw)
        if norm['reason'] or norm['qualifiers'] != qualifiers or norm['comparison_key'] != event['new_comparison_key']:
            continue
        rows[change_id] = {**event, 'qualifiers': qualifiers, 'value': value}
    return rows


class PublicationInputs:
    def __init__(self, root):
        self.root = root
        self.names = [rm.CONTENT + name + '.json' for name in ('venues', 'venues_en', 'closed_days')]
        self.ko, self.en, self.closed = [rm.read_json(root / name) for name in self.names]
        entries = [(value['slug'], name) for name, value in self.ko.items() if not name.startswith('_')]
        self.slugs = dict(entries)
        if len(self.slugs) != len(entries) or set(self.slugs) != {slug for slug in self.en if not slug.startswith('_')}:
            raise rm.GateError('apply', 'ambiguous_inventory')
        self.datasets = {name: get_csv(root, name) for name in ('boxes.csv', 'saunas.csv')}
        self.rows = {kind: {row['name']: row for row in records}
                     for kind, (_, records) in self.datasets.items()}
        if any(len(self.rows[kind]) != len(records) for kind, (_, records) in self.datasets.items()):
            raise rm.GateError('apply', 'ambiguous_inventory')

    def venue(self, slug):
        name = self.slugs[slug]
        record, en = self.ko[name], self.en[slug]
        return record, en, self.rows[record['csv'] + '.csv'][name]

    def allowed(self, slug, registry, *, identities=(), urls=()):
        name = self.slugs[slug]
        record, en, row = self.venue(slug)
        names = [name, slug, record['title'], en['title'], *identities,
                 *(record.get('aliases') or []), *(en.get('aliases') or [])]
        official = [row.get(k) for k in ('website', 'booking_url', 'cf_listing_url',
                                        'cf_official_url', 'hyrox_official_url')]
        return not rm.excluded_venue(registry, names, (*official, *urls))

    @staticmethod
    def price_column(record):
        return 'drop_in_price_krw' if record['csv'] == 'boxes' else 'price_krw'

    def state(self, slug, field, key):
        if field == 'closures':
            return {'closure': deepcopy(self.closed.get(slug))}
        record, en, row = self.venue(slug)
        matches = [index for index, (label, _) in enumerate(en['facts']) if key == label]
        if len(matches) > 1 or len(record['facts']) != len(en['facts']):
            raise rm.GateError('apply', 'managed_slot_conflict')
        fact = [record['facts'][matches[0]], en['facts'][matches[0]]] if matches else None
        state = {'fact': fact, 'evidence': record.get('price_hours_evidence', {}).get(key)}
        if field == 'price_day_pass':
            state['amount'] = row[self.price_column(record)]
        return deepcopy(state)

    def write(self, slug, field, key, state):
        if field == 'closures':
            if state['closure'] is None:
                self.closed.pop(slug, None)
            else:
                self.closed[slug] = deepcopy(state['closure'])
            return
        record, en, row = self.venue(slug)
        if state['fact']:
            (ko_key, ko_value), (_, en_value) = state['fact']
            put_fact(record, en, key, ko_key, en_value, ko_value)
        else:
            for index, (label, _) in enumerate(en['facts']):
                if label == key:
                    del en['facts'][index], record['facts'][index]
                    break
        evidence = record.get('price_hours_evidence', {})
        if state['evidence'] is None:
            evidence.pop(key, None)
            if not evidence:
                record.pop('price_hours_evidence', None)
        else:
            record.setdefault('price_hours_evidence', {})[key] = deepcopy(state['evidence'])
        if field == 'price_day_pass':
            row[self.price_column(record)] = state['amount']

    def restore_inventory(self, managed):
        for slot in managed:
            if 'inventory' not in slot:
                continue
            slug = slot['slug']
            name, korean, english = slot['inventory']
            if slug not in self.slugs and name not in self.ko and slug not in self.en:
                self.ko[name], self.en[slug], self.slugs[slug] = deepcopy(korean), deepcopy(english), name
            elif self.slugs.get(slug) != name or [self.ko.get(name), self.en.get(slug)] != [korean, english]:
                raise rm.GateError('apply', 'managed_slot_conflict')

    def project(self, db, candidates, proposals, mapping, previous, registry, today):
        self.restore_inventory(previous)
        managed = {(slot['slug'], slot['field']): slot for slot in previous}
        carried = {cid for slot in previous for cid in slot['change_ids']}
        events = current_events(db, candidates.keys() | carried, today)
        identities = Counter(mapping.values())
        mapped = defaultdict(list)
        for venue, slug in mapping.items():
            mapped[slug].extend(venue)
        for slug in self.slugs:
            if not self.allowed(slug, registry, identities=mapped[slug]):
                raise rm.GateError('consent', 'excluded_venue')
        notes, superseded, selected = set(), set(), defaultdict(list)
        usable = {}
        for cid, candidate in sorted(candidates.items()):
            event = events.get(cid)
            if not event:
                superseded.add(cid)
                continue
            venue = event['venue_id'], event['venue_key']
            if venue != (candidate['venue_id'], candidate['venue_key']) or event['field'] not in candidate['fields']:
                raise rm.GateError('generation', 'generation_invalid')
            slug = mapping.get(venue)
            if slug not in self.slugs or identities[slug] != 1:
                notes.add(('unmapped', cid))
                continue
            urls = event['requested_url'], event['final_url']
            if not self.allowed(slug, registry, identities=venue, urls=urls):
                notes.add(('excluded_venue', cid))
                continue
            if candidate['review_reasons']:
                notes.add(('review_required', cid))
                continue
            if event['field'] == 'closures':
                usable[cid] = slug
            else:
                selected[slug, event['field']].append((event, [cid]))

        for proposal in proposals:
            if proposal['action'] != 'update' or proposal['state'] != 'closure_notice':
                continue
            ids = proposal['change_ids']
            if not ids or not (slug := usable.get(ids[0])) or not all(usable.get(cid) == slug for cid in ids):
                continue
            support = [events[cid] for cid in ids]
            statements = ' · '.join(sorted({event['value'] for event in support}))
            if (proposal['site_slug'] != slug or proposal.get('review_reason')
                    or proposal['source'] != 'Official page'
                    or not all(changes.closure_state(event['value']) == 'closure_notice' for event in support)
                    or any((event['venue_id'], event['venue_key']) != (proposal['venue_id'], proposal['venue_key'])
                           for event in support)
                    or proposal['closed'] != statements or proposal['checked'] != max(e['checked'] for e in support)
                    or not any((e['requested_url'], e['final_url']) == (proposal['url'], proposal['resolved_url'])
                               for e in support)):
                raise rm.GateError('generation', 'generation_invalid')
            closure = {key: proposal[key] for key in ('closed', 'url', 'checked')}
            closure['source'] = 'Official page'
            if until := change_feeds._until(statements):
                closure['until'] = until
            entry = (closure, ids)
            if entry not in selected[slug, 'closures']:
                selected[slug, 'closures'].append(entry)

        slots, applied = [], set()
        for slug, field in sorted(managed.keys() | selected.keys()):
            prior = managed.get((slug, field))
            options = selected[slug, field]
            if slug not in self.slugs and field != 'closures':
                raise rm.GateError('apply', 'managed_slot_conflict')
            if prior:
                key = prior['key']
                if self.state(slug, field, key) not in (prior['base'], prior['applied']):
                    raise rm.GateError('apply', 'managed_slot_conflict')
                base = prior['base']
            elif field == 'closures':
                key, base = 'Closure', self.state(slug, field, 'Closure')
            else:
                record, en, _ = self.venue(slug)
                keys = {regular_slot(field, option['qualifiers'], record, en) for option, _ in options}
                key = keys.pop() if len(keys) == 1 else None
                if key is None:
                    notes.update(('unsupported_scope', cid) for _, ids in options for cid in ids)
                    continue
                base = self.state(slug, field, key)

            accepted = []
            if options:
                if field == 'closures':
                    same = all(option == options[0][0] for option, _ in options)
                else:
                    same = all((option['value'], option['qualifiers']) ==
                               (options[0][0]['value'], options[0][0]['qualifiers']) for option, _ in options)
                if same:
                    value = max((value for value, _ in options), key=lambda v: (v['checked'], rm.canonical(v)))
                    if field == 'closures':
                        self.write(slug, field, key, {'closure': value})
                        accepted = sorted({cid for _, ids in options for cid in ids})
                    else:
                        record, en, row = self.venue(slug)
                        if regular_slot(field, value['qualifiers'], record, en) == key and price_or_hours(value, key, record, en, row):
                            accepted = sorted({cid for _, ids in options for cid in ids})
                if not accepted:
                    notes.update(('conflicting_or_incompatible_slot', cid) for _, ids in options for cid in ids)

            if accepted:
                applied.update(accepted)
            elif prior:
                support = [events.get(cid) for cid in prior['change_ids']]
                if support and all(e and mapping.get((e['venue_id'], e['venue_key'])) == slug
                                   and identities[slug] == 1 and self.allowed(slug, registry, identities=(e['venue_id'], e['venue_key']),
                                                    urls=(e['requested_url'], e['final_url'])) for e in support):
                    self.write(slug, field, key, prior['applied'])
                    accepted = prior['change_ids']
                else:
                    self.write(slug, field, key, base)
            if accepted:
                slots.append(dict(slug=slug, field=field, key=key, base=base, change_ids=accepted,
                                  applied=self.state(slug, field, key)))

        closed = self.permanent_closures(today, registry, slots)
        for slot in slots:
            slug = slot['slug']
            if slot['field'] != 'closures' or slug not in closed or slug not in self.slugs:
                continue
            # Keep restored inputs in the release record for a later withdrawal.
            for other in slots:
                if other['slug'] == slug and other['field'] != 'closures':
                    self.write(slug, other['field'], other['key'], other['base'])
            name = self.slugs[slug]
            slot['inventory'] = [name, self.ko.pop(name), self.en.pop(slug)]
        slots = [slot for slot in slots if slot['field'] == 'closures' or slot['slug'] not in closed]
        return slots, closed, applied, superseded, notes

    def permanent_closures(self, today, registry, slots):
        closed = rm.closure_slugs_from_records(self.closed, today)
        sealed = {slot['slug'] for slot in slots if slot['field'] == 'closures'
                  and slot['applied']['closure'] == self.closed.get(slot['slug'])}
        reviewed = {slug: row['official_urls'] for row in registry if row['decision'] == 'allowed'
                    for slug in row['slugs']}
        return sealed & closed | {slug for slug in closed if self.closed[slug]['url'] in reviewed.get(slug, ())}

    def materialize(self):
        updates = {}
        values = (self.ko, self.en, self.closed)
        for name, body in zip(self.names, values):
            path = self.root / name
            if body != rm.read_json(path):
                updates[name] = json.dumps(body, ensure_ascii=False, indent=2) + '\n'
        for name, (fields, records) in self.datasets.items():
            path = self.root / name
            if records != get_csv(self.root, name)[1]:
                stream = StringIO(newline='')
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(records)
                updates[name] = stream.getvalue()
        journal = {}
        for name, content in updates.items():
            path = self.root / name
            journal[name] = {'before': rm.digest(path.read_bytes()), 'after': rm.digest(content.encode()),
                             'text': content}
            path.write_bytes(content.encode())
        return journal
