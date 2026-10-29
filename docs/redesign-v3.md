# HANSET v3 implementation and review notes

Design input: `/workspace/redesign-2026-10/hanset-v3/design-v3.md`, read in full,
and its approved `mockup.html`. Owner visual approval: 2026-10-26. Local stack
parent: `76b7b74` (`redesign/hanset/pr5-metadata-separators`). No network Git,
push, remote PR, issue, merge or deploy is part of this build stage.

## PR1 — read-only catalog and evidence consumer (draft, publication disabled)

Branch: `redesign/hanset-v3/pr1-public-catalog`.

`site/public_catalog.py` uses AST literal extraction, never operational imports.
Entity and facility vocabulary and required sections come from the supplied
ontology file. A small English presentation descriptor filters supported facets;
new kinds remain unavailable to positive matching. Layout is a list, never a
facility checkbox. City/type options accept eligible metadata only, not a guessed
domain or natural-language classification.

`site/public_slots.py` prepares a proposed version-1 operations export reader.
The envelope is `schema_version`, timezone-aware `cutoff`, operations-reviewed
`branches` mapping public slugs to branch IDs, and `slots`. Slot fields follow
design §4. The branch bridge must be injective and refer to existing public slugs.
The consumer rejects unknown schemas, duplicate JSON keys, unmapped slots,
incomplete approved facts, bad official provenance, invalid intervals and prices
without explicit qualifiers. Unspecified qualifier values remain unspecified.
False and zero survive; source claims, AI and unverified candidates lose their
values and provenance. Conflicts and stale evidence never become affirmative.
Closure expiry uses KST and does not assert reopening. Raw quotes and internal
review notes are excluded from the consumer result.

The builder validates available inputs before replacing `dist`. It writes only a
catalog/capability manifest to `assets/public-catalog.json`; publication remains
disabled even if a proposed export is supplied. No new public value or new UI is
enabled by this draft. No CSV, content JSON, operational package, source fact,
Korean string, font or illustration is changed. Existing prepared Latin fonts,
licences and full-page Pretendard subsetting already exist in the parent stack.

Current generated catalog excerpt:

```json
{"schema_version": 1, "available": false, "reason": "missing_ontology",
 "sections": [], "facets": [], "publication_enabled": false,
 "input_available": false}
```

## Required decisions and operations inputs

The BASE does not contain `hanset_twin/ontology.py`. Local `origin/main` contains
it, but a whole-branch sync also introduces content updates, operational packages
and a deploy script. This conflicts with the instruction to leave those inputs
untouched and not introduce deploy scripts. AGENTS.md requires owner resolution;
the sync is pending an explicit decision. No operational constants are copied
from another worktree or silently duplicated into frontend production code.

There is no operations-issued `site/content/public_slots.json` or attribute-level
allowlist in this worktree. Design §4 makes the explicit approval contract a
publication dependency, and §5 makes it a PR1/PR7 dependency. The proposed consumer
contract needs operations review before activation. The current mixed-provenance
legacy templates are preserved; this PR does not bless their claims or attempt
an unauthorized content correction.

Prepared operations request (not sent): provide the versioned public slot export
from design §4, exact retained JSON pointers and approved identity bridge;
attribute types/enums/applicability; scoped prices, dated hours/exceptions,
confirmation dates, conflict/quality/rights state; reviewed brand edges and
optional sourced coordinates. Do not substitute collection time for confirmation
or treat internal `source_claim` service views as public evidence.

PR2–PR7 remain pending. Do not create placeholder branches or claim screenshots
of unimplemented templates. Brand pages, nearby pairing, comparisons, dated
answers and history have no invented fallback data. Copy requests and the full
operations-field request are also recorded outside Git in
`/workspace/rd-wt/logs/hanset-v3-copy-todo.md`.

## Visual decision reconciliation

The approved mint/teal light and dark tokens, Archivo semi-condensed headings,
Source Sans 3 body and self-hosted Pretendard fallback remain authoritative.
`docs/longblack-structure-spec.md` explicitly preserves and supersedes the old
paper/terracotta and Home-colour-only history. Its old branch-article structure
and PR6 deferral now refer to the previous stack, not the v3 plan. No HANSET
`DESIGN.md` or `DECISIONS` file exists in this BASE.

## Validation checklist

- [x] Baseline full unit suite: 87 tests, PASS with `TMPDIR=/dev/shm`.
- [x] PR1 full unit suite: 101 tests, PASS with `TMPDIR=/dev/shm`.
- [x] Focused evidence/catalog cases: no code execution, drift, false/zero,
  unknown/conflict/stale, official provenance, price scopes, KST closure expiry.
- [x] Final release build and leak check, PASS (actual output below).
- [x] Home/Finder/Orakai before/after: 390×844 and 1440×900, light and dark.
  All 12 screenshot pairs are byte-identical. Representative mobile/desktop
  images were also opened for visual inspection. Fonts load; no overflow at
  320, 360, 390, 768, 1024, 1440 or 1920; no external requests or browser errors.
  Inherited combined city/type filters and reduced-motion rendering pass.
- [x] Generated internal links/assets, canonical/sitemap/robots, public noindex,
  404, absent ads route, Search Console verification, hashed CSS/JS.
- [x] Exact-diff contact/secret scan and unchanged content/Korean inventory.
  Existing HTML/CSS/JS are byte-identical to the baseline; the only added asset
  is the disabled catalog. Regenerated font timestamps and calendar DTSTAMPs
  differ between builds, as in the inherited builder.

```text
built 69 pages, 49 venues (10 boxes, 39 saunas), 5 events, 2 verified-feed events, 2 creators -> /workspace/rd-wt/hanset-v3/site/dist  (13832 KB total)
leak check OK (no owner credentials / third-party-source names / trackers)
```

Commands: `TMPDIR=/dev/shm python3 -m unittest discover -s tests -p 'test_*.py'`
and `cd site && python3 build_site.py --release`. Both PASS. Foreground Chrome
checks used Playwright with local route fulfillment; no background server was
started. A rejected-export builder test also confirms existing output survives
validation failure. No claim is made that deferred v3 templates are implemented
or that all inherited screens satisfy every new visual gate.

Baseline test invocation without `TMPDIR` failed in 43 collector cases before
any edit: this machine has `/tmp/.git`, so the collector correctly refuses state
under it. `/dev/shm` supplies an external temporary directory and all 87 baseline
tests pass. No collector code change is required. This environment has `python3`
but no `python`; the equivalent release command is
`cd site && python3 build_site.py --release`.

Screenshots and measurement artifacts are in
`/workspace/rd-wt/logs/hanset-v3-pr1/`, outside the repository. Required screenshots
are review artifacts, not temporary files. Disposable test files are cleaned by
`TemporaryDirectory`; browser checks use local route fulfillment and no server.

## PR1 review fixes — 2026-10-29 (still draft)

Both consumer majors are addressed. `evaluate()` now requires a separate
operations contract and actual retained content before returning a value. Missing
contracts or unsupported slot descriptors yield unknown with no candidate value
or provenance; the reader reports `missing_operations_contract`. The builder
reads the proposed `site/content/public_contract.json` alongside the export.
Neither operations input is created by this frontend PR. Publication stays off.

The proposed sidecar has `schema_version: 1`, `descriptors` and `retained` lists.
Each descriptor contains exactly `slot` (entity type/kind/attribute), `value`
(type rule), `unit` and `qualifiers` (key-to-type-rule mapping). Supported type
rules are boolean, integer, finite number, nonempty string, date and explicit
enum, with numeric minimum/maximum and explicit nullable qualifiers. Operations
must supply the actual policy enums, units, bounds and applicable qualifier keys;
this PR adds no production defaults for them. Extra qualifiers, wrong types,
units or enum values, duplicate matching descriptors and unsupported rules are
rejected. Existing price/presence guards also remain in force.

Each retained binding includes `public_slug`, `branch_id`, `entity_type`, `kind`,
`attribute`, `json_pointer`, typed `value`, `unit`, `qualifiers` and `value_text`.
All fields must exactly match the row, including JSON types and price scopes.
Pointers resolve using RFC 6901 against a document keyed by the retained file
names `venues.json`, `venues_en.json` and `foreigner.json`; targets must equal
the original `value_text`. This draft supports retained text targets only.
Missing targets, other-branch bindings, changed text/values/scopes, malformed
escapes and invalid list indexes fail validation before replacing `dist`.
Unsupported structured schedules, bath lists or program semantics stay unknown.
Operations review of this proposed descriptor/binding format remains required.

The dependency blocker is **unresolved**. Local `origin/main` is `fedec85`,
whereas the unchanged parent remains `76b7b74`. The available base has no
ontology or operations-issued descriptor/export. Importing operational files or
inventing approval would not resolve that dependency. A complete parent-stack
sync includes content and a deploy script and still requires the owner decision
described above. Only v3 PR1 exists locally; there are no later v3 branches to
rebase. No parent branch, main, operational content or deploy script was changed.

Fix-stage verification and screenshot checklist:

- [x] Full unit suite: **110 tests PASS**, including nine new regression tests
  for pointers, identity/scope bindings, typed evidence and builder atomicity.
- [x] `cd site && python3 build_site.py --release`: **PASS**, 69 pages,
  49 venues; **leak check OK**. The full output is in `build.txt` below.
- [x] Fresh Playwright screenshots for Home, Finder, Orakai detail and Archive:
  **390×844 and 1440×900, light and dark**, plus Home illustration/footer views.
  All 20 captures live under `/workspace/rd-wt/logs/hanset-v3-shots/`.
  The 12 Home/Finder/detail viewport captures are pixel-identical to reviewed
  PR1; `screenshot-comparison.json` records the decoded-pixel comparison.
- [x] Real browser checks: fonts and theme backgrounds, no overflow at
  320/360/390/768/1024/1440/1920, compound filters, empty/reset and URL reload;
  zero external requests, missing assets or browser errors. `verify.cjs` and
  `validation.json` in that directory reproduce/document the checks. Browser
  routes serve only local release files; no background server is used.
- [x] Generated comparison: 106 baseline files identical; only existing font
  timestamps/calendar stamps vary, plus the previously introduced disabled
  catalog asset. Existing HTML/CSS/JS and user-visible Korean copy are unchanged.
- [x] Exact-diff contact/secret scan and `git diff --check`: PASS. No content
  data, hero, font, illustration, deploy or crawler changes.

```text
built 69 pages, 49 venues (10 boxes, 39 saunas), 5 events, 2 verified-feed events, 2 creators -> /workspace/rd-wt/hanset-v3/site/dist  (13832 KB total)
leak check OK (no owner credentials / third-party-source names / trackers)
```
