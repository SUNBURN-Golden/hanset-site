# Official web changes: internal review only

The existing web collection path records daily official-page observations, then
compares hours, day-pass prices and closure statements offline. No site data,
builder, twin store, browser, Instagram session or schedule is involved. The
existing 04:15 KST wrapper remains unchanged. There is no publishing or sending
command in this feature.

```sh
python3 -m hanset_active collect detect-changes --dry-run
python3 -m hanset_active collect detect-changes
python3 -m hanset_active collect detect-changes --rebaseline
python3 -m hanset_active collect export-changes --as-of 2026-11-10
python3 -m hanset_active collect report
```

These offline commands need no model credentials and ignore unrelated model
configuration. They do no network work. `detect-changes` and `export-changes`
serialize writes under `$HANSET_IG_DIR/daemon.lock`. The writable collector open
adds four `web_change_*` tables to `collector.sqlite3`; claims and historical
provenance are preserved. Read-only commands do not migrate, create directories
or touch WAL read marks: committed database/WAL bytes are copied into memory.
A concurrently changing or corrupt snapshot fails closed. Read-only inspection
of an old or missing schema returns zero change counts. Dry-run also reports
how many existing observations/baselines are visible and whether rebaseline
was requested, including the would_reseed count; it performs no bootstrap,
reseed or export.

## Evidence and transitions

An exact enabled `official_urls` entry in the external `web_targets.json` is
required. A domain-approved search lead alone is insufficient. Requests still
pass the existing robots, block, login and redirect guards. Requested URL and
resolved URL are retained separately. Unsafe URLs (including credentials,
private/denied hosts or redaction markers) are rejected, never repaired into a
different source identity. Loopback URLs are permitted only in collector tests
and never exported as feed evidence.

Only web / own_site / source_claim / internal_review_only claims with
web_verbatim / page_span / verbatim_source=1 provenance can enter observations.
Instagram summaries, third-party evidence and legacy NULL provenance cannot
contribute. Venue id and key must both match. Hours retain scope and facility;
prices retain scope, ticket, audience and duration. Qualifiers are exact,
including the distinction between absent and unspecified. Time padding and
explicit KRW formatting normalize to the established claim comparison rules.
Closure text uses exact grounded statements. Multiple distinct values within
one observation are held as `within_observation_conflict` without advancing its
baseline. Held evidence is recorded as a diagnostic without unsafe values.
Unsafe free text and qualifiers are held, not silently rewritten.

Claims and receipts commit first. An independently committed daily observation
then precedes the daily completion checkpoint. A receipt can recur on a later
day without another model request; its grounded claims are reused for that new
observation. Same-day page/hash/target replay is ignored. A→B→A→B on four days
therefore produces three immutable transitions, with distinct predecessor
observation ids. A bootstrap predecessor instead names its original claim id.

First values seed baselines without events. Formatting-equivalent values are
no-ops. New/missing qualifiers produce diagnostic events, excluded from VIRAL.
An entirely absent field does not imply a closure removal. Refused/empty pages
and failed extraction produce no observation and never clear a baseline.
Changes to target authorization emit review diagnostics and seed a new scope;
values never compare across different authorization fingerprints. Detection also
retires baselines whose authorization was removed, even without a new fetch.
Run detection after editing targets before exporting their review feeds.

Collection recovers pending observations before its work and attempts detection
and export after every return path. Derivation uses a savepoint per observation.
A detector error rolls back derived changes, retains durable claims and the
pending observation, and defers subsequent observations of that source until
retry. Other sources continue. Collection success is preserved and a fixed
side diagnostic appears in the result/status. `report.changes` includes counts,
pending observations, baseline/event counts, diagnostics, version state and
manifest status. No exception text, secret values or credential paths are
included in the review artifacts.

## Bootstrap and explicit rebaseline

The first writable detection run seeds unambiguous authorized stored claims:
all normalized values in each source/field/qualifier scope must agree. Closure
scopes require exactly one distinct grounded statement. Original claim ids,
seen_at and provenance remain intact. Ambiguous groups are counted as
`bootstrap_ambiguous` and wait for fresh observations. No historical events are
invented. Missing or invalid targets defer bootstrap until a later writable run.
Historical deduplicated receipts cannot reconstruct earlier repeated page
observations or A→B→A transitions. Observation history begins with this feature.

A detector/normalizer version mismatch reports `rebaseline_required` and stops
comparison. Run `detect-changes --rebaseline` explicitly to retire baselines,
reseed each authorized scope from its newest eligible observation (or claim if
none exists), mark pending observations `processed=2, diagnostic=rebaselined`,
and increment the rebaseline generation. This emits no events. Existing events
stay immutable; reseeded current scopes have no rebuild divergence until the
next comparable transition. Rebaseline never fetches a page or invokes a model.

## Files and VIRAL consumption

Writable detection/export writes only the external collector state and:

- `$HANSET_IG_DIR/official-web-changes/closed_days.proposals.json`
- `$HANSET_IG_DIR/official-web-changes/rebuild_candidates.json`
- `$HANSET_IG_DIR/official-web-changes/viral_topics.jsonl`
- `$HANSET_IG_DIR/official-web-changes/manifest.json`

The directory is mode 0700; files are 0600. Symlinked output components are
refused. All artifacts declare `rights=internal_review_only` and
`auto_publish_to_site=false`. There is no merge into live `closed_days.json`
or `venues.json`. No output directory is created by report or dry-run, and an
export with no committed events returns `no_events`.

Closure proposals are current per venue, from the latest value event per
source/scope. `closed` appears only on an update with a closure notice.
Statements such as 연중무휴 or “normally open” yield review-only proposals
without `closed`. No removal action is generated. Disagreement across comparable
official sources holds proposals for `source_conflict` and excludes topics.
End dates require one fully specified end date or one fully specified range,
without recurrence or additional date tokens; ambiguous/yearless dates omit
`until`. Statements are sorted, deduplicated and joined with ` · `.

Rebuild candidates list current known divergences, fields, latest event ids,
source URLs and review reasons. They are neither a build command nor a review
ledger. The collector never reads site content or guesses a slug. Optional
mapping is explicit and external, at `$HANSET_IG_DIR/site_slug_map.json`:

```json
{"venue-1":{"venue_key":"example-sauna","site_slug":"example"}}
```

Without a matching id/key mapping, closure `site_slug` is null and
`mapping_status` is 확인 중; rebuild `suggested_routes` is empty.

VIRAL reads the full eligible JSONL log in event_seq order. Each line includes
a deterministic factual blurb, normalized values, qualifiers, source URL,
checked date and stable change_id. Diagnostic events, no-closure statements,
unsafe/unsupported evidence, source conflicts and stale events are excluded.
A latest conflicting or unsafe observation also holds earlier topics for that
scope and turns its closure proposal into review-only.
`requires_review` is always true. No raw pages or contact details enter blurbs.

1. Read `manifest.json`, then verify the exact artifact set, SHA-256 hashes,
   byte lengths and event watermark before parsing any feed.
2. Re-read the manifest after reading the files; retry if it changed or any
   hash fails. `manifest_status()` provides local hash/watermark verification.
3. Deduplicate by change_id and maintain VIRAL's own consumption cursor.
4. Obtain human review before any message send or publication. Reading the
   topic file does not authorize either action.

Canonical bytes use UTF-8, sorted keys, compact separators and a trailing
newline. The manifest's event_digest hashes newline-joined sorted change_ids
(without a trailing newline). No export wall-clock timestamp affects hashes.
Default as_of is the maximum checked date among events, or 1970-01-01 for an
empty artifact set; `--as-of` explicitly ages outputs for ops/tests. Items more
than 90 days older than as_of show 오래됨; closure/rebuild items remain visible,
while stale topics are excluded. With an unchanged event set, baseline review state, mapping and as_of,
re-exported bytes are identical.

Every file is staged in the same directory, fsynced and replaced atomically;
the manifest is replaced last and the directory is fsynced. Handled replacement
failures restore the previous generation and return `export_failed`. Committed
events survive, and a later export regenerates every artifact. Readers must
always verify hashes, including during replacement or after interruption;
a partial generation is never safe to consume merely because a file exists.
