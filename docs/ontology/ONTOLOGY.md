# HANSET venue digital twin — ontology, state rules, daemon

Code: `hanset_twin/` (schema `hanset_twin/migrations/0001_twin.sql`). DB: `$HANSET_TWIN_DIR/twin.sqlite3`
(default `/workspace/hanset-data/twin`, outside the checkout). The active-bot store
(`$HANSET_ACTIVE_DIR/active.sqlite3`) is opened **read-only**; raw documents stay there.

## Entities
`venue` (brand/operator, grouped by registrable domain) → `branch` (one physical location; never merged across names)
→ `facility` (kinds: dry_sauna, wet_sauna, sauna, kiln_sauna, cold_plunge, rapid_cold_plunge, ice_bath, warm_bath, hot_bath, layout),
`price_plan` (ticket × scope × audience × duration), `hours` (regular per scope, closure), `usage_condition`
(foreigner, tattoo, age, access), `program`. Aliases (`aliases`, branch tokens kept) and external ids (`external_ids`).

## Fact record (every value)
id · entity · attribute · value/unit · **status** (`fact` = explicit rule match on the venue's own site;
`source_claim` = directory or curated CSV; `ai_inference` = schema slot only, no writer, never served;
`unverified` = no evidence, value NULL) · source URL · evidence locator (char offsets / CSV line+column / PDF page)
· quote ≤ 240 chars · origin ref (active observation id or CSV line) · collected_at · effective_from/to ·
superseded_by · closed_reason · rights (`public_page_factual` / `internal_review_only` for the curated CSV)
· retention_until (quote/evidence text, 365 days; fact rows keep history) · last_confirmed_at.

## State rules
- One **slot** = entity × attribute × source URL. A new snapshot of the same source with a different value
  **closes** the open period (`effective_to` = new `effective_from`, `superseded_by`, `corrections` row) and appends the new fact.
- Same value re-observed → only `last_confirmed_at` moves.
- Different sources disagreeing, or one page giving several values → `conflicts` (kept side by side for review; nothing overwritten).
- Triggers: facts cannot be deleted, values cannot be edited, a closed period cannot be re-opened.
- Missing required slots get an explicit `unverified` placeholder; evidence closes it (`evidence_found`).
- Ambiguous extractions, unresolved subjects (article/list pages) and same-domain branch candidates go to `review_queue`.

## Daemon (`python3 -m hanset_twin run`, cron `scripts/run_twin_cron.sh`, hourly at :50 after the active bot at :35)
1 discover (sources + rights, candidate branches) · 2 incremental fetch (new active-store documents after the cursor +
changed CSV rows; **no network**, polite fetching stays in `hanset_active`) · 3 change detect (content hash per canonical URL) ·
4 extract (reuses `hanset_active` observations and `hanset_active.extract.fields` rules on CSV text; no model calls) ·
5 entity resolution (exact branch-keeping name or exact website URL only) · 6 evidence verify (https source, not denied,
quote + locator, non-ambiguous) · 7 state update · 8 recompute service views · 9 quality check.
Each stage: checkpoint cursor (`stage_checkpoints`), process flock + stage lease, savepoint rollback on error,
3 bounded attempts then `stage_failures` dead letter (pipeline holds at that stage and resumes there), cost cap 0 KRW
(`cost_ledger` CHECK amount=0; any paid call raises).

## Source coverage (`source_rights`)
Recorded per domain: fields, permission, refresh days, retention, supported flag and reason.
Not collected, stated explicitly: **Instagram** (required source; login wall and no approved Graph API token — no impersonation,
account rotation or proxies), **Naver** (owner rule), robots-blocked sites. Instagram-only CSV evidence is rejected as `denied_source`.
