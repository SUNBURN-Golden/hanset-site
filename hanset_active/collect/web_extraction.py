"""Stream canonical, grounded page fragments into durable page receipts."""
import json
import re
import unicodedata

from . import CollectError, runtime
from .changes import insert_observation
from .config import MAX_TEXT
from .deepseek import MAX_BATCH_ITEMS, encode_payload, fits
from .ground import INSTRUCTION, parse_items, payload_items, redact
from .safety import remove_personal_text
from .store import (FULL_HASH_VERSION, digest, read_claims, record,
                    remember_web_target_observation, validated_authorization, web_receipt)

_SENTENCE_END = re.compile(r'\n|[.!?。](?=\s|[가-힣])')


def page_text(text):
    # Newlines occupy one offset, just like spaces in the normalized hash input.
    clean = redact(unicodedata.normalize('NFKC', text))
    return '\n'.join(line for raw in clean.splitlines() if (line := ' '.join(raw.split())))


def fragments(text):
    safe_text = remove_personal_text(text)
    start = 0
    while len(text) - start > MAX_TEXT:
        end = start + MAX_TEXT
        boundaries = [match.end() for match in _SENTENCE_END.finditer(text, start, end + 1)
                      if match.end() <= end]
        if boundaries:
            cut = boundaries[-1]
        elif text[end].isspace():
            cut = end
        else:
            cut = text.rfind(' ', start, end)
        if cut <= start:
            raise CollectError('model_input_limit')
        yield {'text': safe_text[start:cut], 'offset': start}
        start = cut
        while start < len(text) and text[start].isspace():
            start += 1
    if start < len(text):
        yield {'text': safe_text[start:], 'offset': start}


def batches_fit(items, instruction=INSTRUCTION):
    return len(items) <= MAX_BATCH_ITEMS and fits(encode_payload(items), instruction)


def answer_batch(router, documents, *, split=True):
    try:
        answer = router.ask(payload_items(documents))
        try:
            parsed = parse_items(answer, documents, web=True)
        except CollectError:
            router.reject_last()
            raise
    except CollectError as error:
        if error.reason == 'model_invalid' and split and len(documents) > 1:
            middle = len(documents) // 2
            yield from answer_batch(router, documents[:middle], split=False)
            yield from answer_batch(router, documents[middle:], split=False)
        else:
            yield from (([], [], error.reason) for _ in documents)
    else:
        yield from ((claims, dropped, None) for claims, dropped in parsed)


class WebExtraction:
    def __init__(self, db, ledger, state, router, observe_page):
        self.db, self.ledger, self.state, self.router = db, ledger, state, router
        self.observe_page = observe_page
        self.batch = []
        self.counts = dict(committed=0, pending=0, unchanged=0)
        self.rejections = {}
        self.added = 0
        self.reason = None

    def offer(self, row):
        text = row.pop('text')
        layout = page_text(text)
        row['content_hash'] = digest(layout.replace('\n', ' '))
        key, text_hash = row['requested_url'], row['content_hash']
        # The raw legacy digest proves coverage only for an uncut page.
        # A legacy digest equal to the full normalized hash also proves coverage.
        legacy_hash = digest(text) if len(text) <= MAX_TEXT else None
        index = self.ledger.execute('''SELECT receipt_id FROM extraction_cache
            WHERE source_kind='web' AND requested_url=? AND content_hash=?''', (key, text_hash)).fetchone()
        receipt = None
        if index:
            # The index can also remember a legacy receipt already proven to
            # cover a full page with this normalized hash.
            receipt = self.db.execute('''SELECT * FROM receipts WHERE receipt_id=?
                AND source_kind='web' AND requested_url=?
                AND (hash_version IS NULL OR (hash_version=? AND content_hash=?))
                AND disposition IN ('applied','empty_extraction')''',
                (index[0], key, FULL_HASH_VERSION, text_hash)).fetchone()
        if receipt is None:
            receipt = web_receipt(self.db, key, text_hash, legacy_hash)
        if receipt is not None:
            self._stored(row, receipt, 'unchanged')
            return
        remember_web_target_observation(self.db, row['url'], 'pending_extraction', content_hash=text_hash,
                                        observed_at=row['seen_at'], testing=row['_testing'])
        runtime.retry_web(self.ledger, row, self.state.clock, 'pending_extraction')
        try:
            documents = list(fragments(layout))
        except CollectError as error:
            self._pending(row, error.reason)
            return
        if not documents:
            self._commit(row, [])
            return
        page = {'row': row, 'remaining': len(documents), 'claims': [], 'error': None}
        for document in documents:
            trial = [item for _, item in self.batch] + [document]
            if self.batch and not batches_fit(payload_items(trial)):
                self.flush()
            self.batch.append((page, document))
            if len(self.batch) == MAX_BATCH_ITEMS:
                self.flush()

    def flush(self):
        if not self.batch:
            return
        batch, self.batch = self.batch, []
        documents = [document for _, document in batch]
        for (page, _), (claims, dropped, error) in zip(batch, answer_batch(self.router, documents)):
            for reason in dropped:
                self.rejections[reason] = self.rejections.get(reason, 0) + 1
            page['error'] = page['error'] or error
            page['claims'].extend(claims)
            page['remaining'] -= 1
            if page['remaining'] == 0:
                if page['error']:
                    self._pending(page['row'], page['error'])
                else:
                    self._commit(page['row'], page['claims'])

    def _pending(self, row, reason):
        runtime.retry_web(self.ledger, row, self.state.clock, reason,
                          delay=600 if row.get('_facts_only') else 0)
        self.reason = self.reason or reason
        self.counts['pending'] += 1
        self.observe_page(row['url'], 'pending_extraction')

    def _commit(self, row, claims):
        disposition, ids = record(
            self.db, kind='web', url=row['url'], text_hash=row['content_hash'],
            disposition='applied', claims=claims, venue_id=row['venue_id'], venue_key=row['venue_key'],
            seen_at=row['seen_at'], clock=self.state.clock, requested_url=row['requested_url'],
            final_url=row['final_url'], authorization=row['source_authorization'], domains=row['domains'],
            hash_version=FULL_HASH_VERSION, testing=row['_testing'], facts_only=row.get('_facts_only', False))
        receipt = web_receipt(self.db, row['requested_url'], row['content_hash'])
        self.added += len(ids)
        self.state.data['counts']['claims'] += len(ids)
        self.state.save()
        self._stored(row, receipt, disposition if disposition != 'duplicate' else 'unchanged')

    def _stored(self, row, receipt, disposition):
        from hanset_active.util.urls import is_excluded

        unchanged = disposition == 'unchanged'
        key, text_hash = row['requested_url'], row['content_hash']
        remember_web_target_observation(
            self.db, row['url'], 'duplicate' if unchanged else disposition,
            content_hash=text_hash, observed_at=row['seen_at'], testing=row['_testing'])
        authorization = validated_authorization(
            row['url'], row['final_url'], row['source_authorization'], row['domains'], testing=row['_testing'])
        stored_auth = json.loads(receipt['authorization_json']) if receipt['authorization_json'] else None
        venue = row['venue_id'], row['venue_key']
        same_venue = venue == (receipt['venue_id'], receipt['venue_key'])
        same_binding = same_venue and row['final_url'] == receipt['final_url'] and authorization == stored_auth
        claims = read_claims(self.db, receipt_id=receipt['receipt_id']) if authorization or not receipt['hash_version'] else []
        if not receipt['hash_version'] and claims:
            # Legacy receipts rely on their immutable claim venue IDs.
            same_binding = all((claim['venue_id'], claim['venue_key']) == venue for claim in claims)
        auth_json = json.dumps(authorization) if authorization else None
        with self.db:
            self.db.execute('''UPDATE receipts SET binding_current=0
                WHERE source_kind='web' AND requested_url=? AND binding_current=1
                AND hash_version=? AND (venue_id IS NOT ? OR venue_key IS NOT ?
                    OR final_url IS NOT ? OR authorization_json IS NOT ?)''',
                (key, FULL_HASH_VERSION, *venue, row['final_url'], auth_json))
            if not receipt['hash_version'] or unchanged:
                self.db.execute('UPDATE receipts SET binding_current=? WHERE receipt_id=?',
                                (int(same_binding), receipt['receipt_id']))
        if authorization and same_binding and not is_excluded(
                row.get('name', ''), row['url'], row['final_url']):
            insert_observation(self.db, row, claims)
        with self.ledger:
            self.ledger.execute("INSERT OR REPLACE INTO extraction_cache VALUES ('web',?,?,?)",
                                (key, text_hash, receipt['receipt_id']))
        runtime.complete_web(self.ledger, self.state.data['day'], key, disposition)
        self.counts['unchanged' if unchanged else 'committed'] += 1
        self.observe_page(row['url'], disposition)
