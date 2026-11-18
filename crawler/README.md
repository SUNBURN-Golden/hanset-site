# HANSET Linux local collector

This package is independent of `site/`. It never imports the builder, writes CSVs,
`site/content/`, `data/verified/`, or the legacy pipeline JSON, or publishes anything.
Every report declares `auto_publish_to_site: false`. Operational review and site
publication remain separate work.

## Run

Python 3.11+ and SQLite are sufficient for HTTP collection, intake and reporting.
Run from the repository root:

```bash
python3 -m crawler report --queue data/pipeline/queue.json --format json
python3 -m crawler report --format json
python3 -m crawler --config /outside-repository/approvals.json candidate https://source.test/guide
python3 -m crawler --config /outside-repository/approvals.json worker --once
python3 -m crawler --config /outside-repository/approvals.json serve --bind 127.0.0.1
```

`serve` maintains the extension intake and expiry supervisor. Use `worker --once`
in a separate process for scheduled HTTP/browser jobs; each invocation processes
at most one due body. Run it repeatedly under your local supervisor. Live owners
are preserved when these processes share the database. No scheduler or deployment
integration is installed by this package.

State defaults to `$XDG_STATE_HOME/hanset-collector/collector.sqlite3`, or
`~/.local/state/hanset-collector/collector.sqlite3`. `--state` specifies another
external SQLite file. Repository paths, including symlink targets, are refused.
Read-only reporting does not initialize a database. SQLite transactions reserve
candidate/body/attempt budgets atomically; failed and uncertain attempts are not
refunded. Budgets reset by the worker's KST day; domain holds and backoff do not.

## Explicit approvals

The shipped policy registry is empty, so no source is fetched until approved.
Legacy `sources.json` descriptions confer neither access nor official trust.
Candidate registration and manual clipping of an already open public tab do not
require network approval. Unapproved manual clips remain clues and cannot grant
HTTP/browser access or official trust. Reservations, quotas and privacy checks
still apply before the extension reads any body.
Create an external JSON override, for example with synthetic hostnames replaced
by reviewed sources:

```json
{
  "extension_origins": ["chrome-extension://YOUR_INSTALLED_EXTENSION_ID"],
  "seeds": ["https://source.test/feed"],
  "policies": [{
    "origin": "https://source.test",
    "paths": ["/feed", "/guides/"],
    "disallow": ["/guides/private/"],
    "public": true,
    "browser": true,
    "robots_reviewed_at": "2026-10-23",
    "crawl_delay": 5,
    "follow_links": true,
    "official_venues": ["example-central"],
    "field_rules": [{
      "field": "rates",
      "pattern": "Admission (?P<value>[0-9]+) KRW",
      "scope": {"currency": "KRW", "unit": "visit"}
    }]
  }],
  "venues": [{
    "venue_id": "example-central",
    "branch": "Central",
    "aliases": ["Example KR", "Example EN"],
    "urls": ["https://source.test/guides/kr", "https://source.test/guides/en"]
  }],
  "document_relations": [{
    "id": "reviewed-translation",
    "urls": ["https://source.test/guides/kr", "https://source.test/guides/en"]
  }]
}
```

Path approvals are exact paths or subtrees, not arbitrary string prefixes.
Review robots and access terms locally and record the date (maximum age 90 days),
disallowed paths and any slower Crawl-delay. This is an explicit reviewed robots
snapshot, not an automatic robots.txt parser. Additional origins for scripts/APIs
must have separate approvals. Never approve authentication, paywalls or technical
block bypass. Naver and declined venues remain excluded.

Use a separate path policy with `adapter` set to `rss`, `atom`, `sitemap`,
`crossref`, `pubmed`, `search`, or `links` for discovery endpoints. PubMed accepts
E-utilities JSON `result.uids` or `esearchresult.idlist`; Crossref reads
`message.items[].URL`, search reads `results[].url`. An approved search endpoint
must use an explicit reviewed URL format (generic search/pagination traps are
rejected). `research: true` marks scientific metadata sources; it does not verify
facility facts or generate health claims. No API credentials are required or
handled. Seed depth is zero and discovered links stop at depth two. Cursor
digests and normalized URLs persist in checkpoints; repeated cursors stop.
`recheck_seconds` can set a reviewed source cadence (minimum 300 seconds).

Limits may be lowered: 500 new candidates, 200 new bodies and 200 total attempts
per KST day, one request per domain, at least five seconds between requests.
Redirects, resources, bytes and job duration are also bounded. Volatile rates,
hours and closures are rechecked before new discovery. 429 honors Retry-After,
403/block screens stop the domain, and three consecutive 5xx require operator
intervention. A successful response resets the failure streak. Embedded CAPTCHA
scripts/contact forms do not by themselves count as a blocking challenge.
Disallowed discovered links are skipped individually; a full candidate budget
pauses discovery and saves progress without discarding the fetched parent page.

## Evidence and review

Records retain URL/title/source, nullable publication date with reason, worker
collection time, venue identity, facts, evidence locations, document group,
privacy/schema version and current workflow stage. Missing facts mean `확인 중`;
facts older than 90 days mean `오래됨`. Supported fields are `place_name_kr`,
`place_name_en`, `branch`, `rates`, `hours`, `booking`, `foreigner_conditions`,
`closures`. Each fact has value, scope, quote, locator, evidence ID, source type,
source URL, checked date and its own verification status. Foreigner-condition
keys are exactly:

```text
public_access restriction booking kr_phone card english full_price
scrub_fee last_entry closures age_tattoo_attire
```

Rules extract only from sanitized minimal regions. Optional caller-provided
facts are also schema checked; they cannot set trust or workflow. Blog/news
rates are `clue_rate`; only a local approved official venue source can supply
`official_rate`. Both still require explicit evidence review before verification.
Matching scoped conflicting values retain both evidence IDs and values. Past,
nonoverlapping rate periods are not current conflicts. Rechecks append immutable
observations and replace current facts using worker order, not client timestamps.
An unchanged, fresh selected fact set retains its verified/public-pending review
on recheck, with new observation IDs linked to the selection. Changed evidence,
staleness, revoked approval or unresolved conflicts require review again.

```bash
python3 -m crawler --config /outside-repository/approvals.json show RECORD_ID
python3 -m crawler --config /outside-repository/approvals.json verify RECORD_ID EVIDENCE_ID
python3 -m crawler --config /outside-repository/approvals.json resolve-conflict CONFLICT_ID EVIDENCE_ID reason_code
python3 -m crawler --config /outside-repository/approvals.json publish-pending RECORD_ID
```

Conflict decisions retain review history. Selected facts are revalidated on
public-pending designation; conflicts or staleness return records to review.
Even `publish-pending` writes only the local DB. Research metadata does not use
venue-fact verification to promote scientific claims.

Venue IDs are branch-specific operator mappings. Alias matches additionally
require a matching branch. Document groups use approved translation/syndication
relations, canonical DOI/PMID URLs, or sufficiently long sanitized fingerprints.
Short generic snippets retain independent URL groups. Duplicate records retain
their source, representative ID and facts, including conflicting translations.

## Privacy and browser boundary

HTTP original bytes remain bounded in memory. HTTP, browser and clipper extraction
select the first literal `main` or `article` in document order. No generic root,
`section`, class/ID container or `role="main"` establishes a region. A blocked or
hidden first candidate is rejected rather than replaced by a later one. An
eligible candidate contributes only its own unblocked subtree, including nested
semantic elements; sibling regions and outside links/text never contribute.
The locator is the selected element's `main` or `article` tag. HTTP text separators,
discovered links and explicitly approved archives follow this same scope.

No candidate yields `missing_minimal_region`; a hidden/blocked candidate or
ancestor yields `private_region`; an eligible region without remaining text
yields `empty_region`. Metadata never supplies body evidence, facts or archives.
The sanitized document title remains a separate field and is never appended to
the excerpt; title elements do not contribute HTTP body text.

HTTP checks inherited HTML hiding attributes and inline `display:none` and
`visibility:hidden` (including whitespace/case variations and `!important`), and
conservatively excludes `visibility:collapse`. It cannot evaluate stylesheet
visibility or browser layout. Browser and clipper retain additional computed-style
and layout checks. Shared HTML/inline-style fixtures verify the same semantic
boundary and acceptance/evidence outcomes, rather than identical browser IPC
error strings or parity for CSS/layout and HTML parser repair differences.
Fixtures marked `http_only` cover HTTP-specific conservative filtering and parser
bookkeeping separately.

Forms, editable/hidden areas, private
conversations, frames, scripts and media are excluded. Text, titles, URLs, nested
fields and quotes are filtered again before DB acceptance and AI export.
Public restrictions such as `투숙객·회원 전용` or `members only` are facility facts;
member-only conversations are private context and remain rejected. URL checks use
decoded text, while stored/request URLs use percent-encoded paths and IDNA hosts.
Personal contexts that cannot be safely minimized are rejected with reason codes;
raw rejected text is never quarantined or logged. Regex heuristics cannot prove
arbitrary prose is free of private information; questionable material needs human
review and must not be sent to a model. Injection lines are removed, and the AI
payload has no tools, verification or publication authority. This package does
not call a model or execute page instructions.

Only the HANSET contact survives by default. Exact public representative contacts
may be approved in external `public_contacts`; never put them in repository
config, fixtures or site files. Full text is off by default. An exact URL must
have both `archive_enabled: true` and a `full_text_permissions` approval before
sanitized full text can enter the external archives table. Photos, HAR, video,
browser profiles and screenshots are not collector outputs.

See [Mandatory browser integration](#mandatory-browser-integration) for the
separate browser installation and Linux prerequisites. The Linux browser runner
starts the driver and Chromium in a fresh network namespace with loopback down,
no routes and no external interfaces, and checks this before launch. Profiles
and temporary files live on private tmpfs. The runner aborts on isolation failure
and exposes only bounded stdin/stdout IPC. Browser environment variables are
restricted and no proxy/cookie/token headers are relayed. A host-side gate pins
public DNS-resolved IPs, keeps TLS hostname verification enabled and validates
each redirect and resource before connecting.
DNS answers must all be public: benchmark addresses (`198.18.0.0/15`), their
IPv4-mapped IPv6 equivalents, and mixed public/nonpublic answers are rejected
before constructing a connection. A public address in the same answer never
permits connecting past a forbidden address.
Service workers, frames, popups, WebSockets, downloads, media and authentication
are blocked. Route fulfillment never calls `continue_()` or Playwright fetch.

Extension-submitted jobs additionally require a fresh permission challenge before
start and each outbound request. The extension answers through the fixed intake
using `chrome.permissions.contains`; a stored grant alone never suffices. The
extension's consent responder runs briefly after submit/resume. If it terminates,
goes offline or loses host permission, new challenges fail closed without opening
a destination connection. Resume renews the responder; it grants no body budget.

Reservations bind clip/document/URL, KST day, generation and domain epoch. Begin
must succeed before extraction, and previous-day/expired results cannot commit.
Expiry changes authority to revoking; only the trusted supervisor can free domains
after active sockets and recorded process identities have drained. Unknown owners
stay held. PID start ticks prevent signalling reused PIDs. Already committed exact
payload replays return the original ACK; uncommitted late results are rejected.
Manual DOM extraction creates no worker network request or managed browser process;
the user's existing tab is not navigated or terminated by the worker.

## Reports and tests

The five exclusive current stages are candidate, source checked, deduplicated
out, verified, and publish pending. Unique venues per stage are **not additive**.
Milestones and quota lifecycle counts are separate. Legacy reports derive 44
candidates (three missing URLs) and 31 previously published venues from individual
inventory; legacy summary-only values are never treated as processing evidence.
Discovery indexes have separate operational progress (`discovery_indexes`) and
do not add placeholder evidence to the five-stage or identity counts. Unapproved
scheduled candidates appear in `access_holds`; a hold is audited only when its
reason changes, and is reconsidered when local policy changes. Robots-review
age checks use the same injected worker clock as quotas and evidence.

Run each suite from the repository root. Use a Python 3.11+ test virtual
environment with `pytest` for the Python suites. Keep test and browser
dependencies separate from the static site's build environment.

### Linux crawler unit tests

Use Linux with Python 3.11+, SQLite and writable temporary storage.

```bash
python3 -m pytest -q tests/test_collector_pipeline.py tests/test_minimal_html.py \
  tests/test_store_lock.py tests/test_url_include.py
```

### Node clipper tests

Use Node.js with its built-in test runner.

```bash
node --test tools/chrome-clipper/tests/*.test.mjs
```

### Active collector tests

Install the Python dependencies with
`python3 -m pip install 'requests[socks]' beautifulsoup4 lxml Pillow`. The SOCKS
extra is required for the proxy tests. Use Linux with Bash and `flock` for the
cron tests. Install `pdftotext` and Tesseract with English and Korean language
data to exercise PDF and OCR coverage.

```bash
python3 -m pytest -q -rs tests/active_bot
```

### Twin and explicit evidence tests

Use the active collector test environment.

```bash
python3 -m pytest -q tests/twin
```

### Data foundation tests

Install the data foundation requirements in the test environment.

```bash
python3 -m pip install -r data_foundation/requirements.txt
python3 -m pytest -q tests/data_foundation
```

### Mandatory browser integration

Provision a Linux host with Chromium's OS libraries, util-linux `unshare` and
`mount`, enabled user, network, mount and PID namespaces, procfs, and support
for mounting tmpfs in the user namespace. Install iproute2, OpenSSL, Xvfb with
`xvfb-run` and `xauth`, and `xdotool`. Install the browser requirements and
Chromium from the Python test environment. Place the virtual environment and
Chromium installation outside `/tmp` because the browser runner mounts a
private tmpfs there.

```bash
python3 -m pip install -r crawler/requirements-browser.txt
python3 -m playwright install chromium
```

```bash
python3 -m pytest -q tests/integration
```

Run this suite for every browser or clipper change. Missing prerequisites fail
the run. The suite uses a disposable outer namespace with synthetic DNS, TLS
servers and destination packet/connection monitoring. It exercises real
redirects, JS navigation, frames, popup/fetch requests, service workers, rebinding,
IPv4/IPv6, interception absence, gate failure and missing isolation. Coverage
includes approved-host benchmark A records, mapped benchmark AAAA records,
and mixed public/nonpublic DNS answers through HTTP and browser requests,
redirects, JS navigation, fetch and script subresources. Reachable benchmark
sinks and packet monitoring verify rejection before connection; fixture DNS
answers and gate rejection reasons are also checked. Synthetic addresses, DNS,
certificates and listeners exist only inside the disposable test namespace.
The extension test drives an actual optional-permission prompt, offline intake,
lost begin/ACK, service-worker termination and permission withdrawal. The test
asserts that the browser case exits successfully and reports
`forbidden_connections=0 forbidden_packets=0`.

For the static site, see [Release build verification](../README.md#release-build-verification).
