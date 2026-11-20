# Shared Tavily search and discovery

This file is not a browser daemon. Session-file Playwright collection is not
supported. Web collection and the desktop Instagram handoff are in
[COLLECTOR.md](COLLECTOR.md).

This checkout has a paused Instagram connector, not a DeepSeek browser. The
`hanset_active.search.discover_known_venue` add-on is the optional Tavily hook.
[COLLECTOR.md](COLLECTOR.md) may call it when a web target asks, and otherwise
leaves the known-venue hook optional. Broad active-bot discovery also uses the
same private client through `discover.search.Tavily`, first in the default search
provider order when credentials and shared allowance are available. The hook itself
does not fetch Instagram, start a browser, or publish.

## Callable contract

```python
from hanset_active.search import discover_known_venue

outcome = discover_known_venue(
    db, venue_id, gap_field,
    target="official_page",  # or "instagram_handle"
)
```

`db` is an existing `hanset_active.db` SQLite connection with row access by name.
`venue_id` must identify an existing, nonexcluded venue, and `gap_field` must
identify one of its existing open gaps. Unknown identities, filled/nonopen gaps,
unsupported fields/targets and invalid names are skipped before credential or
budget access. The stored name includes the branch; the query also includes the
stored city and one allowlisted target phrase. Each call sends at most one query
of 240 characters and returns at most three deduplicated leads. There is no new
venue invention, query expansion, pagination, automatic retry or second search.

The immutable `SearchOutcome` has `status` (`ok`, `skipped`, `error`), a fixed
`reason`, `venue_id`, `gap_field`, a tuple of `SearchLead` objects and an optional
`BudgetSnapshot` (`day`, `used`, `daily_limit`, `remaining`). Identifiers may be
absent when validation fails. A successful empty result is `ok` with no leads.
`SearchLead` contains only sanitized `url`, `title`, `snippet`, optional `handle`,
and `verification="unverified"`. Neither results nor outcomes contain credentials,
request objects, HTTP bodies or exception text. The hook emits no logs itself.

Leads are candidates for official-source review, not confirmation of ownership.
They cannot fill facts, create venues, promote publication state, or grant access.
Instagram leads accept only HTTPS profile URLs on Instagram's actual hosts;
posts, reels, login routes and lookalikes are rejected. Handles remain metadata.
The Instagram fetch denylist and connector gate remain in effect. Official-page
candidates use existing URL exclusions, denylist, PII and private-address checks.
Review and access approval must happen separately before any later fetch.
The add-on never writes the active DB, raw documents, site files, crawler state,
clipper state or daemon checkpoint.

Broad discovery separately calls `search.tavily.discover_query` after validating
a bounded query; it does not invent a venue or gap to call the known-venue hook.
Only the `official_page` filtering path is available to broad discovery. Its
sanitized leads become `Hit` hints with empty identity fields and ordinary trial
tasks carrying Tavily provenance. Search results never enter extraction or fact
storage. The executor records only credential-free dispatch metadata and numeric
directory progress in its existing SQLite tables. Missing credentials, provider quota exhaustion, cooldowns and other temporary provider failures leave unfinished jobs
pending without adding failure attempts. Successful empty queries can complete.
Provider-derived retry times clear when eligibility recovers. OSM directories
prefer one Tavily query per region/type combination and resume from their cursor;
Nominatim remains the fallback. Page fetching and robots enforcement are separate
from authenticated search. No Instagram handling changes.

## Executable caller example

Run from the checkout after provisioning the external database and state path.
This performs a real search only when the selected venue has an eligible gap,
credentials are valid and the provider is available. Tests inject `clock` and
`transport`; production callers omit both. The transport seam has the signature
of `requests.Session.post`, returning a streaming response with `close()`.

```sh
umask 077
set +x
export HANSET_IG_DIR=/workspace/hanset-data/ig
python3 - <<'PY'
from dataclasses import asdict
import json
from hanset_active.config import Settings
from hanset_active.db import connect
from hanset_active.search import discover_known_venue

with connect(Settings().data_dir / 'active.sqlite3') as db:
    gap = db.execute("SELECT venue_id, field FROM gaps WHERE state='open' ORDER BY gap_id LIMIT 1").fetchone()
    if gap:
        outcome = discover_known_venue(db, gap['venue_id'], gap['field'])
        print(json.dumps(asdict(outcome), ensure_ascii=False))
    # Continue unrelated daemon steps after a skip, error or empty result.
    print('unrelated_step_ready')
PY
```

Persist only the safe outcome if needed. Never log environment contents, request
arguments/headers, responses, exception objects or transport debug dumps.
`tests/active_bot/test_tavily_discovery.py` exercises a mocked daemon tick that
continues unrelated work after missing credentials and provider quota exhaustion, while
leaving the gap open and all venue/fact/publication records unchanged.

## Credentials

The only sources, in order, are:

1. `/workspace/.secrets/tavily_key`: a regular, nonsymlink file with exact mode
   `0600`, containing only the key (surrounding whitespace is trimmed).
2. An already-set `TAVILY_API_KEY`, only when that file does not exist.

Tokens must contain 8–256 ASCII letters, digits, underscores or hyphens. An
existing unreadable, insecure, empty or malformed file skips search with
`tavily_key_invalid`; it never silently falls back to another credential.
No available credential returns `tavily_key_missing`. Neither skip consumes a
request. Loading a valid file exports its value into this process's
`TAVILY_API_KEY`. There are no key parameters, CLI flags or profile fields.
Tests substitute the file path and environment without reading the real secret.

Python loads the file at runtime, so wrapper changes are unnecessary. A wrapper
that already exports credentials must set `umask 077` and disable tracing with
`set +x` **before** accessing them, as with `deepseek.env`; it must never print
environment values. Search is optional: a missing key does not halt the run.
If a future daemon makes search mandatory, that daemon must handle the skip
explicitly without printing sensitive configuration.

## Persistent usage ledger

`HANSET_IG_DIR` is required and must resolve outside the checkout, including
symlink resolution. Missing or invalid configuration returns
`tavily_budget_unavailable` and sends no request. The directory is created with
private permissions when absent. The add-on owns only `tavily_usage.json` and
its stable `tavily_usage.lock`, independent of the future daemon's
`checkpoint.json`. Both files are mode `0600`; keep the directory private and
share it across every process and restart.
The active-bot discovery provider uses this exact directory and lock as well;
there is no separate discovery credit pool. Advisory availability checks acquire
the lock without reserving credits or rewriting usage. The subsequent locked
reservation remains authoritative against races between callers.

Example state (no credential data):

```json
{
  "schema_version": 2,
  "budgets": {
    "tavily": {
      "days": {"2026-11-05": {"used": 12, "reported_credits": 12}},
      "last_reason": "ok"
    }
  }
}
```

The ledger records attempts and provider-reported credits by KST calendar day.
It migrates schema version 1 and retains its historical counters. Every dispatch
reserves first, including failed, timed-out or interrupted requests. A crash
before dispatch can conservatively count an attempt. KST midnight starts a new
counter. Keep and back up these files alongside external daemon state.

A process-local lock and stable Linux `flock` serialize reservations **through
network dispatch**. Fork workers before calling the hook; do not fork while it
holds its process-local lock. State is validated, then incremented using a private
temporary file, file `fsync`, atomic replacement and directory `fsync` before the
request. The KST date is rechecked immediately before dispatch; rollover reserves
on the new day too, retaining the original reservation. A rapidly changing clock
skips safely. Corrupt state, duplicate JSON keys, lock failure or failed writes
prevent dispatch. If saving the post-request reason fails, the reservation
remains spent and the caller receives `tavily_budget_unavailable`.

A provider credit error records `tavily_provider_quota` and stops Tavily
for the discovery or collection invocation. An ordinary HTTP 429 records
`tavily_rate_limited` and persists a cooldown using Retry-After. The cooldown
accepts seconds or HTTP dates and is bounded to 1–3600 seconds (default 60).
Use the free plan with paid usage disabled. Billing changes require 준태님's
approval.

## Transport and fixed reasons

The private requests session disables environment proxy/netrc inheritance,
redirects and automatic retries. Authentication is request-scoped; the endpoint
is fixed to `https://api.tavily.com/search`. Connect/read timeouts are 5/10 seconds,
streaming is limited to 128 KiB with an additional 30-second body deadline.
No response goes through raw-document persistence. The [Tavily Search API
options](https://docs.tavily.com/documentation/api-reference/endpoint/search)
explicitly select `search_depth="basic"`, `auto_parameters=false`, `max_results=3`,
and disable answers, raw content and images. Search snippets remain untrusted.
Exact credential echoes are removed before PII filtering; credential-bearing
URLs are discarded. Raw transport exceptions are replaced by fixed outcomes.

| Reasons | Caller action |
| --- | --- |
| `ok` | Review any unverified leads separately; continue the run. |
| `tavily_invalid_venue`, `tavily_unknown_venue`, `tavily_unsupported_gap`, `tavily_gap_missing`, `tavily_gap_resolved`, `tavily_unsupported_target`, `tavily_venue_excluded`, `tavily_invalid_identity` | Skip ineligible work without spending allowance. |
| `tavily_key_missing`, `tavily_key_invalid` | Skip optional search; inspect credential provisioning privately. |
| `tavily_invalid_query`, `tavily_run_limit` | Broad discovery rejects invalid input or pauses at its run allowance/deadline without dispatch. |
| `tavily_provider_quota`, `tavily_cooldown`, `tavily_clock_changed` | Continue other steps; search can be considered at a later eligible time. |
| `tavily_budget_unavailable` | No new dispatch; inspect external state/permissions privately. An earlier reserved request may already have failed or completed. |
| `tavily_http_error`, `tavily_rate_limited`, `tavily_timeout`, `tavily_transport_error`, `tavily_invalid_response`, `tavily_response_too_large` | Attempt remains counted; continue without immediate retry. |

## Offline validation

```sh
python3 -m pytest tests/active_bot -q
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tools/chrome-clipper/tests/*.test.mjs
python3 site/build_site.py --release
git diff --check
```

All Tavily tests mock transport. They cover credential precedence, secret echoes,
cap/restart/concurrent processes, KST rollover and clock rollback, crash after
reservation, failed writes, malformed state, URL filtering and unchanged DB
records. No browser behavior or collector/site integration is changed. Building
for validation does not authorize deployment.

Broad-discovery tests additionally cover provider ordering/pins and recovery,
shared collector/discovery contention, run accounting/deadlines, pending search
and directory jobs, budget races, resumable OSM queries, and hints-only storage.
The global active-bot fixture redirects the Tavily key path, clears inherited
credentials, uses temporary external usage state, and blocks external networking.
The publication daemon runs focused discovery/scheduler/runner/Tavily tests first,
then the suites and release build above, captures actual page counts and
`leak check OK`, and runs `git diff --check` and a redacted diff contact/secret
scan. No live Tavily smoke test is required.
