# Stage D discovery interfaces and behaviour

All existing Stage A–C entry points remain available. No new dependencies or
schema migration are required.

- `discover.sitemaps.expand(context, url, *, depth=0, parent_url=None,
  max_children=20, max_candidates=60, slug_include=(), slug_exclude=(),
  via="sitemap", _seen=None, _children=None) -> list[str]`:
  include/exclude expressions match the URL-decoded path case-insensitively,
  before candidate registration. The candidate allowance is shared across the
  whole directory traversal; at most 20 child sitemaps and depth 2 are visited.
  Existing candidates do not exhaust the allowance. Valid `lastmod` timestamps
  contribute urgency (recent updates rank first). Sitemap persistence never
  independently registers entries.
- `discover.directories.discover(context, directory) -> list[str]` passes
  `slug_include`, `slug_exclude`, and `max_candidates` through to the adapter.
- `discover.directories.detail(content, url, profile) -> dict | None` parses only
  configured `directory: true` hosts (including their subdomains). The result
  contains `name`, `website`, `address`, `directory_id`, `low_prior`, and
  `document`. Labelled website links take precedence over other external links;
  denied, SNS, and booking URLs cannot become the official website.
- `discover.search.usable_providers(db, *, clock=None, names=None) -> list[str]`
  defaults to `tavily, nominatim, wikipedia, brave, ddg_html`, respecting explicit
  order and existing inaccessible/disabled states. Tavily rechecks its shared
  credential loader and non-reserving budget availability; missing/invalid keys
  and budget holds expose only fixed reasons. Credential or budget recovery
  clears provider-derived task deferrals, preserving unrelated retry times.
  Missing Brave credentials do not enable a request.
- `discover.search.Tavily(getter, db, *, clock=None, context=None, transport=None)`
  adapts the shared client's sanitized official-page leads to `Hit` objects with
  Tavily/unverified metadata and empty name/address/website identity fields.
  These hints register ordinary trial candidates; snippets never become venues,
  observations, facts, raw documents or official trust. Later fetches still
  enforce robots, access, exclusion and PII rules. Naver, Instagram and RUNPORT
  remain excluded.
- `search.tavily.discover_query(query, *, clock=None, transport=None,
  dispatch_guard=None, on_dispatch=None) -> SearchOutcome` validates nonempty,
  printable queries up to `MAX_QUERY=240` before credentials or reservation.
  Excluded or PII-bearing queries are rejected. Authentication, bounded POST,
  response sanitization and the existing `locked_budget` stay inside the client.
  Credential-free hooks check discovery allowance, request limits, deadline and
  heartbeat immediately before dispatch, then record a single fixed endpoint
  request-log row, including failed dispatches. No authenticated request object
  or search body reaches the page fetcher or evidence pipeline.
- `runner.replenish_search(fetcher) -> None` maintains at most 200 pending or
  running search rows. Every enqueue path shares the same ceiling. Search tasks
  are picked by the existing scheduler score; no profile searches are generated
  when no provider is usable.
- `Scheduler(db, *, clock=None, budget=100, shares=None, discover_floor=.25,
  trial_limits=None, provider_defaults=None)` adds optional per-host per-run
  limits and matching eligibility for injected executor providers. Provider-blocked
  work is deferred to its provider's next time without consuming an allocation,
  attempts, requests, or failure rows. Discovery allocations flow to remaining
  directory/link/trial work.

Pending generic searches and query-directory jobs need no migration. Explicit
provider pins remain authoritative. OSM directories prefer Tavily for one query
per region/type combination, with no OSM endpoints, offsets or pagination in the
POST; Nominatim remains the fallback. The existing run checkpoint table stores
only a numeric combination cursor so interrupted directories resume after their
last completed query. Successful empty responses complete a query; temporary
unavailability leaves work pending without failure attempts. Sitemaps, RSS and
HTML directory adapters retain their existing paths.

Collector and discovery share `HANSET_IG_DIR/tavily_usage.json` and its dispatch
lock. `DAILY_LIMIT=30` is unchanged: basic search permits at most 930 requests in
a 31-day month across these callers. Eligibility consumes no credits; the locked
reservation is authoritative even if another caller spends the last allowance
after eligibility. Exhaustion defers until the next KST day; persisted cooldowns
retain their retry time. Corrupt/unavailable state fails closed, and failed or
interrupted reservations are never refunded. This does not meter unrelated
account consumers. See [IG_DAEMON.md](IG_DAEMON.md) for the shared client contract.

Directory details persist explicit identity/address/website and field evidence
as `official_directory` observations. Venues use their own website domains,
never the shared directory domain. Frozen seed matching remains authoritative.
Official websites become executable trial tasks; SNS/booking/excluded links are
recorded, and applicable gap strategies create bounded follow-up tasks. Naver
and Instagram remain unfetched. New candidates always have `kind='trial'`;
recovery repairs legacy URL tasks with missing kind. Domain evaluation precedes
regular link expansion to depth 2.

The default profile enables VisitSeoul KR/EN sitemap indexes with bilingual slug
filters and 60 candidates per directory. Completed directory roots are revisited
on subsequent runs, continuing past previously registered entries. ID-only
VisitKorea roots are disabled. Retained candidates from these low-prior hosts
are limited to five fetches per run and must pass detail evaluation before
creating venue evidence.

Field rules version 1.1 adds Korean hour-unit ranges, labelled closure lines,
English AM/PM ranges, entry-price vocabulary, policy variants, and table-header
context with row selectors preserved.

## Design/runbook change notes

These notes are inside the authorized implementation paths. Direct edits to
`docs/active-bot/DESIGN.md` and `RUNBOOK.md` await clarification of the conflicting
path restriction. The preceding paragraphs describe the brief discovery and
scheduler updates for those documents.

Offline verification: `python3 -m pytest tests/active_bot -q`.
CLI smoke: use an external disposable data directory with `init`, then
`run --dry-run`, and confirm request_log remains empty. A dry run does not
bootstrap tasks or contact providers. Live discovery testing belongs to the
reviewer; preserve all existing robots/challenge/provider blocks.
