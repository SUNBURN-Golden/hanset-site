# Gap-priority plan

The existing 04:15 KST web run prepares a frozen daily ordering and an operator
IG read list. No new cron is needed. This collector never publishes, requests
Instagram, discovers accounts there, or opens `collector.sqlite3` for planning.
It does not change the static generator, Linux collector/clipper, site facts,
or operator-summary provenance. Priority scoring uses official evidence.

```sh
python -m hanset_active collect plan-gaps
python -m hanset_active collect plan-gaps --ranking local
```

Both commands use the existing `HANSET_IG_DIR`, `HANSET_ACTIVE_DIR`, model
allowlist and `daemon.lock`. The default ranking mode is `model`. Status output
contains counts and fixed reasons only, without handles, prompts, provider
responses or credentials. `testing=True` selects local ranking unless a ranker
is explicitly injected. The planner session is independent of extraction.

## Scoring and identity

Candidates are effective web targets plus enabled schema-1 watchlist entries.
Web targets combine the manual `web_targets.json` with owned URLs derived from
nonclosed active-store venues that have an open/conflict gap in an applicable
priority field still unresolved by `project_needs`. Fresh verified evidence can
therefore suppress stale gap rows. No lifecycle rows or identities are edited.

Derivation uses the venue's HTTPS website (preserving its path), then HTTPS
roots of its recorded domains. The current `seed_freeze` checkpoint also
supplies public HTTPS pages regardless of gaps. An exact normalized venue name
or alias matching exactly one venue ties the seed to that identity. An
unmatched seed keeps its own stable identity. The shared-platform domain alone
cannot tie identities. HTTP seed websites are skipped, and a domain-only seed
may supply its approved HTTPS root. Seed URLs pass credential, PII,
private-host and Instagram guards. Public portal, OTA, blog and declined-brand
pages enter fetch scope only. Derived targets do not enable Tavily.

Web identity resolves by exact `(venue_id, venue_key)` first, then the union of
exact normalized names/aliases and venue website/domain matches. Only a unique
union resolves. Unmatched/ambiguous manual targets stay visible with row reason
`identity_unknown` and plan diagnostics `manual_identity_unmatched` or
`manual_identity_ambiguous`. Watchlist identities still require the exact DB
pair; handles never resolve identity. Unknown identities/types get
`gap_count: null` and cannot enter the IG list. Closed venues cannot enter IG
and sort last for manual web entries. Excluded watchlist entries are skipped
with `watchlist_excluded`. Korean keys are accepted. Only the `venue_id` field
has a narrow SHA-256 ID exception to generic PII redaction; configured secrets
inside those IDs remain forbidden.

Manual targets take precedence per canonical identity and canonical URL.
Their explicit URLs, domains and Tavily spec are preserved. Additional
venue-owned gap URLs must fit the manual domain authorization. Third-party
seed URLs can augment fetch scope without extending official authorization.
Well-formed identity pairs from disabled rows suppress that venue's derivation,
using the same conservative identity resolution. Other malformed disabled data
is ignored as before. Enabled manual rows win over disabled selectors.
All enabled manual identities remain authorized. The 100-target frozen plan
uses manual identities first. Remaining presentation capacity is
filled in local gap-count/importance/identity order. Fixed count-only diagnostics are
`derived_http_only`, `derived_domain_denied`, `derived_suppressed` and
`derived_truncated` (including when a full manual file leaves zero capacity).

`gaps.project_needs` is a SELECT-only projection of the existing lifecycle;
it creates no parallel gap store. The planner opens only `active.sqlite3` via
SQLite `mode=ro`, `query_only=ON` and one read transaction. Missing/unreadable
stores produce `active_db_missing`/`active_db_unreadable` and a usable manual
fallback with unknown identities. All ownership, seed, alias, projection and
evidence fingerprint reads share this transaction. Effective targets and rows
are materialized before closing it; planning and local execution ordering reuse
that snapshot, with no transaction held across ranking or fetch I/O. Website,
domains, aliases and tied seed inputs enter the snapshot digest. Authorization
fingerprints cover identity, official URLs and official domains.
No migrations or lifecycle writes run.

Exactly six profile field IDs are scored: `hours`, `price_day_pass`,
`sauna_temp_c`, `cold_bath_temp_c`, `tattoo_policy`, `foreigner_policy`.
Profile `applies_to` controls applicability: fitness venues have hours,
day pass and foreigner rules, without sauna/bath/tattoo fields. Each unresolved
applicable field counts once. Fields sort by importance descending, then ID.

Evidence authority, in order:

1. Only active-store `observations.label_status='fact'` can qualify. Use the
   latest fact per source URL, with observation ID as deterministic tie breaker.
   Source types `own_site`, `official_directory` and legacy NULL qualify;
   explicit other source types do not. Different hashes from distinct
   qualifying sources mean `evidence_conflict`. Duplicate observations never
   add to the field count.
2. A single hash requires a matching current (`valid_to IS NULL`) fact in
   `venue_fields`, including venue, field, observation ID and value hash.
   `apply_observation` keeps the existing current row when a re-observation has
   the same value, so an older observation ID is accepted while it still matches
   a latest fact for its source URL. Invalid current evidence is unresolved. A
   missing/unparseable aware `valid_from` is invalid.
   `scheduler.refresh_days(field)` expiry or a due
   gap `next_at` means `refresh_due`. A fresh valid current fact overrides a
   stale unresolved gap, recording `stale_gap_state`.
3. Without qualifying current evidence, existing unresolved lifecycle states
   count. A filled gap without valid matching fact/current evidence is invalid;
   its `obs_id` cannot independently establish verification. Claims, including
   legacy NULL-label claims, never close a gap. No gap or qualifying fact means
   `missing_fact`.

| Reason | Meaning |
| --- | --- |
| `gap_conflict` | Known conflict without verified current fact |
| `gap_not_extracted` | Field cue has not been extracted |
| `gap_inaccessible` | Source inaccessible |
| `gap_absent_in_source` | Coverage exhausted, field still unverified |
| `gap_open` | Open lifecycle gap |
| `evidence_conflict` | Latest qualifying source facts disagree |
| `evidence_invalid` | Filled/current evidence fails verification rules |
| `non_official_source` | Only explicitly nonqualifying facts exist |
| `refresh_due` | Freshness expired or gap is due |
| `missing_fact` | No qualifying fact or gap |
| `gap_unknown` | Unknown lifecycle state, treated as unresolved |
| `identity_unknown` | Identity cannot be matched |
| `venue_type_unknown` | Applicability cannot be established |
| `venue_closed` | Closed venue, excluded from IG |

Each field has one primary reason. Fact conflict wins; a fresh verified fact
wins over stale gap state. Otherwise known unresolved states precede missing
or invalid evidence, followed by invalid/nonofficial evidence, freshness,
missing facts and unknown states. None of these diagnostics publish facts.

## Ranking and model identity

More gaps always rank before fewer. Local ties use importance sum descending,
venue ID, then venue key. The model may break ties within the same gap count
for the first 100 actionable candidates. The request includes anonymous
indices, field IDs, channel IDs and gap counts. The web collector drains the
full current authorization set after applying the plan's ordering.

The collector asks Qwen4-Max first. DeepSeek gets the same anonymous payload
only after a Qwen operational failure or missing Qwen credentials and a fresh
healthy check. Web ranking and extraction share provider health during the run.
Every dispatch is logged in the model usage ledger. No daily allowance is
set aside for ranking or extraction. [Collector routing](COLLECTOR.md#model-routing-and-accounting)

The only accepted response is `{"order": [0, 1]}`, an exact permutation of
the supplied integer indices. Invalid replies, unavailable providers, and
missing credentials fall back to local order with a fixed `fallback_*` reason.

Plans and decision caches record the producing model in `ranking.model` and
`model`, respectively. Frozen plans with a valid DeepSeek model remain
readable. A digest-verified decision is reusable regardless of which registered
model produced it. The primary model forms part of the cache key, so upgrading
may rerank once when no same-day frozen plan or matching decision exists.

## Artifacts, freeze and recovery

All paths below are relative to `$HANSET_IG_DIR`, outside the checkout:

| File | Purpose |
| --- | --- |
| `plans/<day>.json` | Schema-1 frozen daily plan |
| `plans/decisions/<model_input_digest>.json` | Validated anonymous decision mapped to venue identities |
| `plans/.incomplete-<day>.json` | Input/target digest marker written before ranking |
| `watchlist-proposals.json` | Already-fetched official-link evidence for ops review |

Plan directories use `0700`; artifacts use `0600`, atomic replace and fsync.
Symlinked files/directories are refused without following or replacing them.
Corrupt, oversized (over 512 KB), unknown-key or inconsistent plans are rebuilt.
Validation checks schema/policy, date/timezone/window, known field/reason IDs,
unique targets, positive occurrence counts, IG identities/handles and cap,
internal references and the plan digest. `auto_publish_to_site` is always false.

A plan records `plan_id`, `input_digest`, `policy_version`, target-set digest,
`targets_status` (`ok`, `missing`, `empty`, `invalid`), KST day/timestamp,
ranking mode/model/reason/decision digest, ranked venue identities with count,
fields/reasons/channels/state, ordered web target IDs/configured URLs and
occurrences, `ig_window`, `ig_read_list` and bounded fixed-code diagnostics.

The digest covers policy, window date, evidence/gap snapshot, watchlist,
target set (including file order) and key fields. Ranking mode/model are also
included in the model input digest. The accepted decision cache contains only
validated venue order, model, policy and its digest. A matching incomplete
marker plus that cache resumes after interruption without another model call.
A mismatching marker is replaced. Without a valid cache a new bounded decision
is attempted or falls back locally.

The first completed plan, including a local fallback, stays frozen. Later
inputs or ranking modes cannot rewrite that day. Ops must have explicit owner
approval before manually removing a plan to rebuild it. The checkpoint remains authoritative for Instagram quotas and security halts.
The active data directory's collector runtime ledger tracks web completions
and model dispatches. Planning adds its own model attempts to accounting.

The target-set digest identifies the frozen default config; drift is reported
at replay, never used as permission to fetch plan URLs. Execution always uses
the freshly assembled effective targets. Removed/disabled targets disappear; new
or changed target IDs append in canonical local order. Diagnostics are
`targets_changed`, `removed_targets`, `unplanned_targets`. No enabled current
target is filtered. URL order within a target is preserved. Exact duplicates
share one effective job in default assembly. Direct planner callers and ad-hoc
files retain the existing duplicate-instance `occurrences` behavior. IDs hash
identity, ordered URLs, effective domains and Tavily spec. The planner never searches.

`collect web --targets other.json` does not read/write daily plans or call the
ranking model. It orders that config locally and reports
`adhoc_targets_local_order`; it never derives targets. Missing/empty manual files
allow default derivation. An invalid file blocks all derivation because its
disabled selectors cannot be trusted, while still preparing the IG handoff
before returning `targets_invalid`. `targets_status` records the manual-file
status independently of effective targets. Completed plans remain frozen,
including earlier zero-candidate plans; new current targets still append at
execution through replay drift diagnostics.

Change detection assembles identical authorization without ranking or plan
writes. If either the manual file is invalid or the active DB is missing,
unreadable or fails midway through its snapshot, it passes `None` to the change
detector: `targets_invalid`, baselines untouched, observations pending for retry.
A readable DB with no eligible venues is authoritative (possibly empty), so
removed derived authorization can retire normally. Collection alone may use
manual fallback on DB failure; detection must defer rather than treat that
fallback as removal of all derived authorization. Explicit ad-hoc authorization
uses only that file and does not depend on the active DB.

## IG operator handoff

| Invocation | Window date |
| --- | --- |
| Web run, at any time | Today |
| `plan-gaps` before 13:00 | Today |
| `plan-gaps` during hour 13 | Reuse today; create once only if none exists, never rewrite |
| `plan-gaps` at/after 14:00 | Tomorrow (`prepared_tomorrow`); today stays frozen |

`ig_window` is exactly 13:00–14:00 KST (`Asia/Seoul`); ingest still accepts hour
13 only. A late web plan carries `ig_window_passed` and must not be consumed.
The list contains at most `min(20, 20 - ig_reads)` enabled, resolved, nonclosed
watchlist venues with gaps, deduplicated by venue and handle. A current halt
makes it empty (`ig_halted_today`). A plan written for another day starts from
that day's fresh quota of 20 and never inherits today's `ig_reads` or halt,
because both are day-scoped. It is never padded with discoveries.

The file is a review handoff, not authorization: before consuming it, the
operator/desktop checks the live watchlist, date/window, current quota and
halt/security state. Skip removed/disabled entries; do not substitute accounts
or replenish a frozen list. The existing operator-summary batch ingest remains
the only IG input. No follows, posts, session files, caption/photo storage or
browser automation are added.

## Watchlist proposals and ops application

Only exact-authorized pages that passed the guarded fetch and `classify_page`
checks are inspected. Requested and resolved URLs must share an approved
official domain. Full sanitized HTML anchors include footer links, with no
additional request. Query/fragment-bearing evidence page URLs are omitted.

Accept only canonical HTTPS `instagram.com/<handle>/` or
`www.instagram.com/<handle>/` anchors with valid handles. Discard posts/reels,
reserved routes, lookalike hosts, userinfo, queries, fragments and non-HTTPS
links. Off-domain or blocked pages are never inspected. No handle is guessed;
no IG request or search snippet/model suggestion supplies discovery evidence.

One venue linking exactly one handle is `eligible`. A handle linked by multiple
venues is `rejected/shared_handle`. A venue linking multiple handles is
`needs_review/ambiguous_handle_count`. Same-day evidence is merged across web
runs and reclassified before writing; earlier days are not current evidence.
Only bounded identity, profile URL, official requested/resolved page URLs,
domain, observed timestamp, content hash and fixed status/reason are stored.
No page body, anchor text, caption, photo or model explanation is retained.

If a snapshot exceeds the metadata bound (1000 entries or 512 KB), retained
rows become `needs_review/snapshot_overflow`. They stay ineligible for the
rest of that day; truncation cannot conceal a shared-account association.

1. Run normal web collection and review `watchlist-proposals.json`.
2. For each eligible entry, manually open its official page, confirm the link
   resolves to that venue's official Instagram profile, and confirm identity.
3. Copy only `enabled`, `venue_id`, `venue_key`, `name`, `handle` into the
   ops-owned `$HANSET_IG_DIR/watchlist.json` (schema 1). Do not copy evidence
   metadata or add `follow_allowed`. Set `enabled` explicitly and take `name`
   from the approved venue configuration; evidence rows carry identity and
   handle only. This code never writes the live watchlist.
4. New entries enter the next unfrozen plan. Today's list stays frozen.

`data/ig/watchlist-proposals.example.json` is synthetic and disabled, showing
watchlist keys separately from evidence metadata. It is not a live proposal or
an input file. Tests use temporary validate directories and mocked DeepSeek;
they never write the live database/watchlist or make external requests.
