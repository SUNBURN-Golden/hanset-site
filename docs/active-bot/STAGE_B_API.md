# Stage B integration interfaces

All database arguments use Stage A SQLite connections. HTTP getters use the
Fetcher.fetch interface; inject Discovery.get into search providers to share
the run budget. Discovery paths produce task IDs. The executor owns task leases.
All clocks accept the Stage A Clock/FakeClock interface. No functions publish data.

## hanset_active.extract.fields

```python
extract(text: str, *, locator=None, ocr_confidence=None) -> list[dict]
extract_document(document: dict) -> list[dict]
```

## hanset_active.labeling

```python
ModelHook()
ModelHook.infer(self, *args, **kwargs)
claim_status(source_type, *, ambiguous=False, hosted_on_own_domain=False, ocr_confidence=None)
labels(text, *, source_type='third_party', content_type='', **kwargs)
store_observation(db, venue_key, evidence, *, source_url, source_type, raw_id=None, extraction_id=None, clock=None, own_domain=None)
store_labels(db, raw_id, extraction_id, items, clock=None)
```

## hanset_active.venues

```python
normalize_name(name)
seed(db, records=(), *, paths=(), approved_sources=None, refreeze=False, clock=None)
is_new(db, name, website='')
upsert_venue(db, name, *, website='', city='', region='', venue_type='sauna', discovered_via='', clock=None)
apply_observation(db, venue_id, obs_id, *, clock=None)
review(db, kind, venue_id, details, clock=None)
```

## hanset_active.discover

```python
Discovery(fetcher, *, getter=None, budget=100, per_domain=12, threshold=0.25)
Discovery.get(self, url, *, kind='body', **kwargs)
Discovery.candidate(self, url, *, text='', name='', city='', region='', website='', depth=0, via='search', parent_url=None, parent_task_id=None, low_prior=False)
Discovery.search_task(self, name, *, region='', type_term='sauna', depth=1, via='mentions', parent_url=None)
```

## hanset_active.discover.search

```python
Hit(url: str, title: str = '', snippet: str = '', name: str = '', address: str = '', website: str = '', extra: dict = <factory>) -> None
SearchProvider(getter, db, *, clock=None, endpoint=None)
SearchProvider.search(self, query, lang='ko', limit=10)
Nominatim(getter, db, *, clock=None, endpoint=None)
Wikipedia(getter, db, *, clock=None, endpoint=None)
Brave(getter, db, *, clock=None, endpoint=None)
DDGHtml(getter, db, *, clock=None, endpoint=None)
queries(profile)
discover(context, providers, *, limit=10)
```

## hanset_active.discover.directories

```python
discover(context, directory)
```

## hanset_active.discover.sitemaps

```python
expand(context, url, *, depth=0, parent_url=None, max_children=20, max_candidates=12, _seen=None, _children=None)
robots_sitemaps(context, origin)
expand_feed(context, url)
list_pages(context, url, *, max_pages=8, url_filter='', low_prior=False)
```

## hanset_active.discover.mentions

```python
names(document)
discover(context, document, *, parent_url='', depth=0, region='')
```

## hanset_active.discover.links

```python
discover(context, document, *, parent_url, venue_id=None, depth=0, source_state='registered')
```

## hanset_active.discover.reverse

```python
followups(db, venue, field, documents=(), *, strategy, clock=None, depth=0)
```

## hanset_active.evaluator

```python
shingles(text, n=5)
jaccard(left, right)
evaluate(url, documents, *, existing_texts=(), seed_domains=(), known_entities=(), known_fields=(), blocked=False, venue_name='', profile=None)
trial_task(db, url, *, clock=None, discovered_via='evaluator')
trial(context, url, *, venue_name='')
record_doc_kind(db, kind, *, raw_id=None, clock=None)
```

## hanset_active.gaps

```python
ensure_gaps(db, venue_id, *, clock=None)
record_attempt(db, gap_id, strategy, *, url='', state='done', task_id=None, reason='', clock=None)
advance(db, gap_id, documents=(), *, clock=None)
resolve(db, gap_id, pages=(), *, exhausted=False, clock=None)
reopen(db, gap_id, *, clock=None)
```

## hanset_active.scheduler

```python
refresh_days(field, *, deprioritized=False)
urgency(staleness_days, field, **kwargs)
score(task, weights=None)
split_budget(total, shares=None, discover_floor=0.25)
enqueue(db, *, kind, url='', task_class='discover', depth=0, payload=None, parent_url=None, parent_task_id=None, discovered_via='', venue_id=None, source_id=None, dedupe_key=None, clock=None, **metrics)
Scheduler(db, *, clock=None, budget=100, shares=None, discover_floor=0.25)
Scheduler.pick(self, *, cooldowns=None, deadline=None)
```

## Integration notes

- Field evidence contains field, value, unit, quote, locator, rule_id, rule_version, extractor_version and ambiguous.
- Use extract_document for Stage A HTML tables, PDF page locators and OCR confidence/boxes.
- Store observations before applying them to venue history. Only fact observations become current service fields.
- seed freezes a snapshot once; refreeze creates a new active snapshot without editing the old snapshot.
- Gap pages supply url, outcome, text and coverage flags (relevant, counterpart, attachment, unavailable, needs_extractor_review). Call resolve with exhausted=True only after strategy exhaustion.
- Complete pending gap_attempts with record_attempt before advancing to the next strategy.
- Scheduler.pick returns a task and accounts for its request share; the executor updates running/done state. Cooldowns and deadlines use monotonic seconds.
- Brave reads its credential from the environment and passes an ephemeral api_headers argument through the injected getter. Fetcher confines that header to the original scheme and host; robots requests receive no credential.
- Stage A integration change: Fetcher.fetch(..., api_headers=None) and its private request helper now support scoped API authentication. Existing call signatures remain compatible.
