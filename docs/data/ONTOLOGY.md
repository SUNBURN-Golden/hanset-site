# Fact ontology and temporal contract

This is an internal, non-publishing fact store for 부티크 사우나 · 호텔 사우나,
fitness drop-ins and events. SQLite supplies single-file transactions, immutable
history enforced with triggers, and `valid_to IS NULL` current-state queries. It
matches the existing collector's SQLite operational model without sharing its DB.
Draft 2020-12 JSON Schema validates extractor output, fixtures and registry entries
before DB intake. Cross-field date ordering also requires `validate_fact`; standard
JSON Schema cannot compare sibling timestamps. SQL independently enforces ordering.

## Entities

All entities use `entities(entity_id, entity_type, parent_id, attributes)`. Required
identity fields are entity_id and entity_type; attributes is a JSON object. Parent
relations are foreign keys; IDs are permanent, never derived from mutable names
after intake. Aliases use `aliases(entity_id, language, alias)`, with ko/en/other.

| Entity | ID scheme | Required attributes / relations | Optional attributes | Tracked state |
|---|---|---|---|---|
| Venue | ven_<slug> | stable identity | ko/en/other aliases | no |
| Branch/Location (Branch) | branch_<slug> | parent Venue | city, district, location_ko, location_en | no |
| Space/Zone (Space) | space_<stable key> | parent Branch | name, floor, zone | no |
| Bath | bath_<stable key> | parent Space | kind hot/very_hot/warm/cold/rapid_cold/other; presence; temperature + unit | yes |
| SaunaRoom | room_<stable key> | parent Space | kind wet/dry/steam/other; presence; temperature + unit | yes |
| Facility | facility_<stable key> | parent Space or Branch | type, accessibility | no, unless availability explicitly observed |
| Price/Ticket (PriceTicket) | priceticket_<stable key> | parent Branch; scoped ticket identity | ticket_type; payer_class guest/non-guest/member/adult/child; day_type weekday/weekend; currency, amount, unit | yes |
| OpeningHours | openinghours_<stable key> | parent Branch | opens/closes, weekday, last_entry, timezone Asia/Seoul | yes |
| Closure | closure_<stable key> | parent Branch | recurring rule text, structured frequency/ordinal/weekday, dated interval, maintenance reason | yes |
| AccessCondition | accesscondition_<stable key> | parent Branch | tattoo, foreigners, age, gender, passport, card, korean_phone_required, public_access, non_guest_access, reservation | yes |
| Program/Event (Event) | event_<stable key> | parent Branch or Venue | program type, start/end, status, booking | yes |

State attributes belong to fact rows, not mutable entity metadata. Missing optional
attributes remain unknown. Legacy tracked subjects are stable per Branch:
`hours_<slug>` (OpeningHours), `closure_<slug>` (Closure), `price_<slug>`
(PriceTicket), and `access_<slug>` (AccessCondition). Attributes and qualifiers
scope individual claims; ko/en renderings share subject, attribute and canonical
Korean label, with qualifiers.language differing. Tips use pointer_key to retain
individual claim identity. Future observations use these same scoped identities.
No Bath, Space or SaunaRoom is invented from an
ambiguous description. Program and room descriptions remain text-only Branch
claims until resolved. Payer/ticket/day qualifiers default to unspecified, never to
adult, day-pass or non-guest without evidence. Amounts in legacy CSVs mean only
an unspecified visit ticket. A comparison requires identical explicit qualifiers.

## Fact row fields

The following machine-tested list is the complete fact-row column contract:

- `fact_id`
- `subject_id`
- `subject_type`
- `attribute`
- `qualifiers`
- `value`
- `value_text`
- `unit`
- `value_state`
- `source_url`
- `source_type`
- `evidence_locator`
- `collected_at`
- `valid_from`
- `valid_to`
- `status`
- `conflict_group`
- `supersedes_fact_id`
- `correction_of`
- `collection_permission`
- `retention_policy`
- `retention_until`
- `retention_days`
- `extractor_id`
- `run_id`
- `created_at`
- `closed_reason`
- `closed_by_fact_id`

`value` is typed JSON (SQL serializes it as JSON text), nullable for text-only
known claims. `value_text` preserves original wording; `{price}` substitution
uses the CSV amount. `unit` holds a known measurement unit, otherwise null.
`qualifiers` is a canonical JSON object delimiting ticket, payer, day and language
scope. `subject_type` names the entity type. `evidence_locator` is nonempty JSON:
quote, CSS selector, heading/page section or repository file + JSON pointer.
`collected_at` and `created_at` are ISO8601 timestamps with +09:00; exact legacy
hours are unknown and represented at midnight. `valid_from` null means unknown
start; `valid_to` null means still current. Observation knowledge begins no earlier
than collected_at when querying historical snapshots of unknown-start facts.

`status`: verified_fact = official-page-confirmed; source_claim = attributed lead
or reply needing verification; ai_inference = model proposal, never publication;
unverified = unknown or unsupported. A verified_fact requires official_page and a
source_url. Source types additionally include official_social, venue_reply,
operator_profile, third_party, ai_extraction and legacy_site_content. Null source
URLs are permitted for non-web replies and legacy/untrusted items whose web
provenance is unavailable or excluded, and for official_social/operator_profile
when the row cannot be attributed to the venue-level URL. These null-URL rows
must have status other than verified_fact. verified_fact always requires
official_page plus a URL. The importer retains mismatched venue-level URLs in
evidence_locator.venue_level_source_url with source_scope venue_level_legacy.

`collection_permission`: public_page_factual permits minimal public factual
excerpts after policy review; venue_reply_internal permits internal reply-derived
facts only, with a 365-day review; manual_review_only is intake without collection
authority. These enums do not grant network permission. `retention_policy` is
facts_keep_history, review_365_days or limited_raw. retention_days and/or
retention_until express review/expiry schedules. Review does not authorize deleting
immutable history: a future minimized export/rebuild policy must handle erasure
without bypassing triggers. Raw captures have independent, shorter retention.
`extractor_id` and `run_id` identify provenance and deterministic execution.

Unknown/not-applicable facts have null value and status unverified. Missing,
Not confirmed, Not stated, not listed and awaiting reply remain unknown; unknown
is not “no limit”, “unlimited”, allowed or a negative policy. Empty or unlimited
stand-ins are rejected. `match_known` returns unknown separately; a tattoo query
cannot match unknown as accepted and must show “확인 중” / “Not confirmed”. Text-only
known claims are not machine-resolved permissions. Facts older than 90 days show
“오래됨”; re-verification uses the latest confirmation date, retaining the original
observation. Only current, non-conflicting verified_fact with evidence and a check
within 90 days can support a definitive answer.

## Append-only state and conflicts

Facts never overwrite: BEFORE UPDATE allows only a single NULL-to-timestamp
valid_to closure plus closed_reason and closed_by_fact_id; DELETE is rejected.
All other columns stay byte-identical. A change inserts a successor with
supersedes_fact_id and closes the old interval in one transaction. A correction
sets correction_of on the new row and closes the old row with reason corrected.
`observe` matches exact source_type + source_url lineage and canonical qualifiers.
Identical values append fact_confirmations with the new source URL, source type,
status and evidence; those rows cannot
be updated/deleted. Unknown observations append separately and never close known
facts. New, different-source values create an open conflict review item and keep
both source values open. Because existing rows cannot acquire a conflict_group,
the earlier fact is closed with reason conflict_grouped and an equivalent grouped
replica supersedes it. Both open representations share conflict_group. Review
fact_ids refers to these open representations. This preserves provenance without
an exception to append-only enforcement. No automatic winner is selected.

Human resolution adds a new fact, references correction_of/supersedes_fact_id,
closes each losing/open representation with resolution reason, and records
review_items.state resolved/dismissed with resolution_notes in one transaction.
The stage design requires operator authority for resolution; observe does not
resolve reviews. `current_facts` excludes closed and superseded rows;
`state_changes` retains all state transitions. `current_state`, `state_history`
and `changed_since` expose JSON-decoded observations, including conflict groups.

## Tracked state vs static relations

A tracked state object has an identity, a state vector with validity intervals,
an observation source and a change-detection rule. A plain relation graph is not
a digital twin. Phase 1 implements generic observation/state tracking; it does
not claim live physical mirroring or install a running daemon.

| Tracked entity | State vector | Change rule |
|---|---|---|
| OpeningHours | schedule, last entry | scoped schedule differs |
| PriceTicket | amount, currency, unit, ticket/payer/day | same ticket scope differs |
| Closure | recurring rule, dates, maintenance window | rule/interval differs |
| Bath | published temperature, availability | known scoped value differs |
| SaunaRoom | published temperature, availability | known scoped value differs |
| AccessCondition | tattoo/non-guest/age/gender/booking policies | policy differs |
| Event | status, dates | status or interval differs |
| Static relations | Venue–Branch, Branch–Space, Space–Bath/SaunaRoom | explicit identity review, no live state claim |
| Static metadata | aliases, geography | operator identity maintenance |

Each tracked entity uses the same tested observe contract: equality confirms,
same-source changes close/append, different-source differences retain conflict,
unknown does not replace known. Presence=false requires evidence, not absence.

## Storage boundary

Default raw store: `/workspace/hanset-data/raw/`, content-addressed snapshots under
source-id subdirectories. Default service DB:
`/workspace/hanset-data/db/hanset_facts.sqlite3`. `HANSET_DATA_DIR` overrides the
common root; `data_paths` and `open_db` reject resolved repository paths, including
symlink targets. No DB/raw data belongs in this repository. The collector keeps
its own `$XDG_STATE_HOME/hanset-collector/collector.sqlite3` or
`~/.local/state/hanset-collector/collector.sqlite3`. No shared writable DB.
