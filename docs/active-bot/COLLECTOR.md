# Web collector and desktop Instagram handoff

This collector writes internal `source_claim` rows under `HANSET_IG_DIR`
(default `/workspace/hanset-data/ig`). It does not publish the site, fill active
gaps, or write the twin. No site output change means no deploy.

The retired session-file Playwright daemon is not implemented. Do not add a
browser profile, cookies, CDP, proxy, or login-challenge bypass. Signed-in
Instagram use stays on the approved desktop. This repository only ingests the
desktop's operator-summary batch.

`hanset_active.connectors.instagram.InstagramConnector` gates the separate
desktop workflow. Web collection accepts public Naver and RUNPORT sources.
Declined listings stay outside publication and outreach workflows.

## What a successful run means

`success: true` means this invocation finished the work it was allowed to do.
`collected: true` means at least one new grounded claim was stored. A process
that starts, locks, and exits is not by itself a collection. `no_targets`,
`missing_credentials`, `outside_window`, and `no_batch` are not collections.

Claims are `status=source_claim` and `rights=internal_review_only`. Verified
first-party web provenance uses `source_type=own_site`. Other public web
provenance uses `source_type=third_party` and displays `미확인` in review.
Instagram claims use `source_type=sns`. Quotes are at most 240 characters.
Raw page text, captions, and photos are not stored. Model reasoning text is
discarded. A new web quote is a page span (`web_verbatim`). A new Instagram
quote is a span of the operator's paraphrase (`operator_summary`), not the
post's original caption. Grounding that span checks the paraphrase only. It
does not verify the post. Rows stored before provenance columns existed keep
NULL origin and are unrecorded, not relabeled.

## Model routing and accounting

Ranking, web extraction, and desktop summary ingestion start with
`qwen4-max-preview` on the existing Qwen Token Plan. The loader reads
`/home/box/.qwen/token_plan_key` or `HANSET_QWEN_KEY_FILE` at runtime. It
refuses symlinks, checkout files, and invalid keys. Qwen disables thinking,
omits `response_format`, and accepts a single JSON fence. The collector
validates the returned JSON locally and discards reasoning.

When Qwen lacks credentials or reports an operational error, the collector
can try DeepSeek using `DEEPSEEK_API_KEY`. `HANSET_DEEPSEEK_MODEL` selects
`deepseek-flash` by default or `deepseek-v4-pro` on the same account. The
DeepSeek fallback requires a healthy result less than six hours old. If
health is missing or expired, one read-only balance probe checks
`is_available`. A negative result is reused for six hours. Insufficient
balance from a probe or completion disables DeepSeek for the invocation,
including web ranking and split retries. A valid Qwen response requires no
DeepSeek request. Model requests use direct sessions with bounded timeouts
and disabled redirects. Approval is required for any plan upgrade or top-up.
[DeepSeek balance API](https://api-docs.deepseek.com/api/get-user-balance/)

`HANSET_ACTIVE_DIR/collect-runtime.sqlite3` holds provider health and a
`model_usage` ledger of dispatched completions and probes. Each entry records
the provider, model, time, outcome, input characters, requested output allowance,
reported token usage, and cost basis. An explicit provider amount with currency
is recorded as `provider_reported`. Unavailable cost is `unknown`. Balances,
keys, headers, and provider bodies are discarded. Checkpoint budgets are
cumulative KST-day counters. They do not gate model calls. Status `provider`
counts dispatched completions in the invocation. Probes appear separately in
the ledger. `deepseek_skipped: "insufficient_balance"` reports a balance hold.

### Web batches and recovery

Web collection uses the guarded active fetcher. The fixed User-Agent includes
`hanset@soulbound.co.kr`. Requests serialize per registrable domain, and each
redirect must pass URL guards before the destination is contacted. Web jobs use
HTTPS except in loopback fixtures. Instagram, asset, and policy pages never
reach web extraction. Fetch and model volume have no per-run or daily page
ceiling. The active lock protects the guarded fetch queue during collection.

The collector hashes the full eligible text after NFKC and whitespace normalization for each canonical
requested URL before fragmenting long pages at line, sentence, or word
boundaries. Each compact JSON request fits six fragments or 24,000 input
characters including the instruction allowance. A fragment carries up to
4,000 characters. The response allowance is 1,200 tokens per item. Strict
local validation requires one result per index and typed claims with grounded
evidence. A malformed batch splits once into two independently validated
halves. The collector waits for all fragment results before deciding whether
to commit the page.

The receipt dispositions `applied` and `empty_extraction` record model
coverage and are cached by canonical requested URL and content hash. A hit
reports `unchanged`, preserves claim IDs and check dates, and records the new
observation. The receipts database lets a restart recover a cache entry after
a cache-index write is interrupted. Legacy short-text receipts count only
when the legacy hash covered the full eligible text. Current venue binding
and guarded final-URL authority govern reuse in review. A `rules_only` receipt
retains the retry. The model pass writes the cache and completion ledger.

The runtime database also stores same-day web completions and retry metadata.
It imports safe same-day web completion keys from the old checkpoint.
Unfinished pages are reauthorized and refetched on resume through the guarded
fetcher. A partial result includes full `counts.committed`, `counts.unchanged`,
and `counts.pending` for the invocation, with up to 200 page outcomes sampled
in status. The ledger retains all completions and retry entries. The checkpoint
continues to track Instagram completions, the read limit and security halts.

### Rule fallback during provider outage

When all failed fragments on a page report provider availability errors
(`model_rate_limited`, `model_insufficient_balance`, or `model_unavailable`),
the collector runs the deterministic field rules over the whole sanitized
page. Explicit provider error codes for quota, exhausted usage, balance, and
unavailability can override the HTTP status. A bare 503 remains a transport
error. Invalid model output, input limits, authentication failures, timeouts,
transport errors, and missing credentials leave the page pending. A page
also stays pending if the rules find no safe, unambiguous claim.

A rules-only commit stores only matched fields. Exact-authorized own-domain
evidence follows the existing manual promotion policy. Third-party evidence
stays `미확인` unless it meets the existing independent-agreement policy.
`extraction_method` and `rules_version` on the claim record extractor
provenance. Fields version 1.9.0 recognizes `1일권` and `타투` across the
collector's model grounding, active evaluation and evidence validation, and
twin CSV extraction.

`counts.rules_fallback` counts pages covered by rules in this invocation,
including repeats. It is available in the run JSON and status. The run keeps
the outage reason and remains `partial` even when `counts.pending` is zero.
Rules-only receipts defer change observations and baselines until model
coverage. When a valid model pass arrives for the same source binding, it
confirms matching claim IDs, adds any new claims, and supersedes omitted rule
claims. Superseded claims remain historical evidence and are excluded from
current assessment and change detection. Daily `claims_new` metrics still use
the receipt creation date, so any new model claims on an upgraded receipt are
attributed to the original rules day.

These receipts live in `collector.sqlite3`. The scheduled twin reads
`active.sqlite3`. A separate handoff is required before the scheduled twin can
ingest the collector's rule evidence.

### Information subpages and URL cooldowns

Web runs select up to two information links per venue identity per invocation,
shared across its initial URLs. Links are read from raw HTML, including navigation,
only when an explicitly authorized first-party page returns `ok` and its
classification is `ready` or `empty_body`.
Keyword scores add together: price/hours and `sauna`, `사우나`, `찜질`, `찜질방`,
`jjimjilbang` weigh 3; facilities/spa and `이용가이드`, `guide`, `floor-guide`,
`층별` weigh 2; general information weighs 1. Ties sort by canonical URL.
Relative links resolve against the final response URL.
Candidates must be HTTPS (HTTP loopback only in tests), share the parent's
registrable domain, belong to the target's approved domains, and pass the existing
portal/exclusion and safe-URL guards. Policy pages, downloads, credential-bearing
URLs and external links are rejected. Tracking parameters and fragments collapse;
meaningful query parameters remain distinct. Canonical identities deduplicate
slash variants and drive queue, completion, and cooldown comparisons. Each
identity keeps its maximum score and first validated fetch URL, preserving the
original encoded path and trailing slash for the actual request.

Fresh children run immediately after their parent, ahead of new roots. All
children use the same guarded fetcher, proxy, robots rules, and locks. Children
never recurse. A child under a block hold is noted as skipped without consuming
one of the two subpage slots. Redirects with unsafe URL identities, including
session parameters, are refused before the destination is contacted.
Fetch totals include robots refusals and collateral domain stops. Link selection
precedes extraction and receipt deduplication, so unchanged or empty-extraction
roots can still yield new children. Pending discovered children resume only
when the parent and scope still authorize their URLs.
Only newly discovered children enable the fetcher's subpage mode: paths stay
intact through normalization, with at most two HTTPS redirects within the
initial registrable domain. External redirects and downgrades are rejected
before destination robots or body requests. Every permitted hop still passes
URL denial checks. News hosts also get robots evaluation against the preserved path. A robots
denial never requests the destination body. Automatic redirects stay disabled.
A shell the fetcher returns as `empty_body` without
HTML cannot yield links; the classifier's `empty_body` fallback can when the
fetcher returned `ok` with HTML.

Discovered children retain the venue identity and approved domains, but inherit
no `source_authorization`. They can create `source_claim` / `internal_review_only`
receipts and claims under their own requested URLs, without automatic change
observations or altered target fingerprints. An already queued explicit URL
promoted into the child position retains its original URL, exact-URL
authorization, and default fetch behavior, even at depth one.
Watchlist proposals require exact-URL authorization and a final URL on the
same approved domain. A proposal is evidence for manual review.

The external collector DB adds `web_target_blocks` without modifying historical
rows. It stores only a SHA-256 hash of the safely canonicalized requested URL,
the terminal disposition, and a timezone-aware timestamp from the collector
clock. A confirmed robots rule denial establishes a hold. `login_wall` and other
terminal refusals establish holds when a response confirms them, including security
text on a 200 response. A later URL stopped collaterally by the fetcher's in-run domain
stop has `blocked` with no status: it still counts this run, but gets no hold and
can be retried next run. A blocked homepage therefore does not create a cooldown
for an independently approved venue subpage.

Holds last 14 days per URL. `recent_block_skipped` makes no fetcher call, consumes
no page slot, and adds no completion. Skips preserve the existing timestamp.
At expiry the URL is eligible and a new genuine terminal result starts a fresh
hold. Expired login and block holds are pruned when opening cooldown state
and in each write transaction. Expired robots holds remain available for policy
maintenance. Malformed entries fail as `state_corrupt`.
Skip notes follow fetch/extraction notes so the 200-entry public status sample
preserves real results; `rejections.recent_block_skipped` reports the total.
When no unfinished URL remains outside cooldown, the result is successful with
`collected: false`, reason `recent_block_skipped`, before proxy preflight or model dispatch.

Search leads must pass both the approved-domain and safe-URL checks. A rejected
identity produces an `unsafe_url_skipped` note without exposing the URL.

Active collection applies robots.txt, Crawl-delay, and a five-second minimum
interval only to Korean news hosts in `hanset_active/profiles/kr_news_domains.txt`.
The collector caches usable robots policies by origin for 24 hours. The request
lock covers all schemes and ports of the exact hostname. Other hosts make one
request at a time with no crawl delay. Sitemap discovery may read robots.txt
for sitemap URLs. Public Naver non-news pages are eligible as third-party source
claims. Naver pages and Korean news outlets are ineligible as official venue
sources. A 429 or 503 stops the hostname for the run. The collector records
Retry-After and resource holds. It releases legacy robots holds only when the
stored effective URL proves a non-news denial and records the release counts
in the run result.

A search-only target can fetch every safe lead in its approved domains. Each
`tavily` venue and field pair retains its own grant even when two searches find
the same page. On a complete authorization scan, a receipt loses its current
binding when its last grant is removed. Search results remain third-party
claims. The shared ledger records usage. Provider quota exhaustion stops Tavily
calls for the invocation. [Tavily ledger](IG_DAEMON.md#persistent-usage-ledger)

### Seven-day soft deferral of unchanged pages

After URL safety, block holds, and same-day completion checks, web execution
uses observations from the past seven days to defer canonical requested URLs
with `duplicate`, `empty_extraction`, or `applied` with a hash matching an
applied web receipt for that URL. At seven days the URL returns to normal
priority. Initial jobs keep their planner order within each queue. The normal
queue drains first and deferred jobs then fetch under the same guards.

Fresh link candidates get first choice of each venue's two subpage slots.
Within each group the original link ranking applies. Fresh children follow
their parent immediately, and deferred children wait behind all normal jobs,
including manual and gap-derived targets. A promoted explicit child retains its
request URL, authorization, and default fetch behavior. An explicit child
already in the deferred queue keeps its place there.

The external SQLite table `web_target_observations` stores the latest fetch
timestamp, disposition, and available content hash under the SHA-256 digest of
the safely canonicalized requested URL. A ready page records
`pending_extraction` at fetch time. A rules-only commit retains that
disposition and normal retry priority while model extraction is unfinished.
A completed model extraction records `applied`, `duplicate`, or
`empty_extraction` with the same timestamp. Both duplicate
paths and discovered children update this history. A failed fetch or unfinished
extraction gives the URL normal priority when it is eligible to retry.

Until a URL has scheduling history, the collector uses its latest safe web
receipt or change observation. A change observation can extend the timestamp
when its canonical URL and hash match a receipt. Receipt creation time is all
that is available for historical repeats without a recorded observation.
Scheduling entries stay available after the seven-day window, and existing
evidence rows retain their original timestamps and labels.

`rejections.recent_unchanged_deferred` reports the number of distinct canonical
URLs admitted to the deferred queue, including jobs fetched later in the run.
The key appears in the run result and `collect status` for positive counts.
Queue admission spends no budget and writes no completion or fetch timestamp.

## Layout

| Path | Role |
| --- | --- |
| `/workspace/hanset-data/ig` | Checkpoint, status, claims DB, inbox, logs. Mode `0700`. |
| `/workspace/hanset-data/ig/deepseek.env` | Optional `0600` file owned by the runtime user. |
| `/workspace/hanset-data/ig/web_targets.json` | Manual official URLs and disabled selectors; merged with active DB gap targets. Not the example in git. |
| `/workspace/hanset-data/ig/watchlist.json` | Official handles allowed in a desktop batch. |
| `/workspace/hanset-data/ig/inbox/*.jsonl` | Transient desktop batch. |
| `/workspace/hanset-data/active` | Robots cache, fetch audit, and `collect-runtime.sqlite3` for health, usage, cache, completions, and retries. |
| `/workspace/hanset-data/twin` | Unchanged by this collector. |

`data/ig/*.example.json` shows the shape with `enabled: false`. Copy a file
outside the checkout before enabling anything. A schema 1 checkpoint from the
old browser draft is refused and left in place (`state_corrupt`).

`deepseek.env` may contain:

```sh
export DEEPSEEK_API_KEY='replace-with-the-host-key'
export HANSET_DEEPSEEK_MODEL=deepseek-flash
```

Do not print, log, or commit it. The wrapper rejects a symlink, a file that
is not mode `0600`, or a file owned by someone else. It sources the file with
`set +x` and discards the file's own output.

Web targets:

```json
{
  "schema_version": 1,
  "targets": [
    {
      "enabled": true,
      "venue_id": "existing-venue-id",
      "venue_key": "existing-venue-key",
      "name": "Example Hotel Sauna",
      "official_urls": ["https://venue.example/hours"],
      "official_domains": ["venue.example"]
    }
  ]
}
```

Default web runs combine manual targets, priority gaps, the current frozen
seed, and promoted discovery observations in one read-only DB snapshot. New
discoveries enter on the next collection run, including after the daily plan
freezes. Manual targets take precedence and disabled identities suppress
derivation. Missing or empty manual files permit derivation. Invalid manual
files block it. An explicit `--targets` selects the ad hoc set.

Verified first-party URLs can supply official authorization. Public blog,
portal, map, directory and OTA URLs are fetch targets with third-party
`source_claim` provenance and `미확인` review labels. RUNPORT's own site can
supply internal evidence. Listing still requires the venue's consent.
Discovery and collection write internal state.

The planner records up to 100 targets. Fetch execution rotates and drains
the full current authorization set. Change detection sees that same set.
A plan over 512 KB fails as `plan_invalid`.
See [GAP_PRIORITY.md](GAP_PRIORITY.md) for ownership/seed ties, identity
resolution, count-only diagnostics, snapshot reuse and DB failure semantics.
Planning/execution can fall back to manual targets on DB failure; change
detection defers instead, retaining baselines and pending observations.
`--targets other.json` remains manual-only and never derives or writes plans.

`official_urls` is the manual activation path. `tavily` is optional and
still requires `official_domains`:

```json
{"tavily": {"venue_id": "existing-venue-id", "field": "hours"}}
```

## Commands

From `/workspace/hanset-site`, after the external files exist:

```sh
HANSET_IG_DIR=/workspace/hanset-data/ig \
HANSET_ACTIVE_DIR=/workspace/hanset-data/active \
HANSET_TWIN_DIR=/workspace/hanset-data/twin \
bash scripts/run_collect_cron.sh
```

The wrapper loads the private `deepseek.env` when present and supervises the
04:15 KST web job. Collection holds the existing overlap locks and has a
25-minute timeout. The wide invocation uses the same web service and
completion ledger, reads the live target set, and picks up new targets and
due retries. The wide wrapper sets up the review handoff when
`HANSET_AUTO_PUBLISH=1` and operations readiness is present. Keep the
existing 04:15 line and use the managed installer after merge.
[Scheduler, stamps and daily metrics](RUNBOOK.md#cron)

The direct CLI remains available for manual web runs and status inspection.

```sh
python3 -m hanset_active collect web
python3 -m hanset_active collect status
```

## Operations publication handoff

Operations runs `scripts/auto_publish.sh --check-readiness --deployed-artifact "$CONFIRMED_RELEASE_DIR"`
with the preserved last uploaded artifact. The check uses temporary stores
and writes
`/workspace/hanset-data/publication/v1/readiness.json` with integer
`schema_version` equal to 1, a nonempty `consumer_version`,
`non_publishing_check` equal to `passed`, and a timezone-aware
`confirmed_at`. The installer requires this file to enable the added
schedule. The watchdog skips wide dispatch when readiness is missing or
invalid.

The collector stages immutable review artifacts under
`generations/<generation_digest>/`, using the SHA-256 of the manifest as the
generation digest. Each manifest identifies the artifact hashes and sizes.
After a durable collection checkpoint, the collector exposes
`requests/<request_id>.json` with `schema_version`, `request_id`, KST
`slot`, `generation_digest`, and sorted unique `change_ids`. Job, slot,
and generation determine the request ID. The wide request retains its v1
shape. An opted-in 04:15 run includes `"job": "daily_collect"`. Replay reuses
the original ID. The supervisor writes a completion signal under
`stamps/<job>.publication.json` after verifying no change or exposing the
request. The normal cron wrapper drains the Outbox when the signal carries its
run token and a request ID. The watchdog can recover a checkpoint. Any requests
it exposes can be drained by the next completed batch or by operations.

Operations verifies the generation and rechecks current official-source
confirmation, source URL, checked date, site venue mapping, and review holds.
Public projection covers regular hours, regular day-pass or drop-in prices,
and official closure notices. Independent-source agreement qualifies
internal facts only under AGENTS.md rule 7. A durable
operations ledger keyed by stable `change_id` keeps overlapping requests
attached to their existing dispositions or in-flight releases. Release
retries use new deployment attempt IDs under operations control. Site
publication stays with the operations bot.
[Official web evidence and review exports](OFFICIAL_WEB_CHANGES.md)

Operations writes immutable terminal receipts under
`receipts/<KST completion date>/<request_id>/<record>.json`, using a
distinct record name for each deployment attempt. The receipt binds
`schema_version`, `request_id`, and `generation_digest` to
`deployment_attempt_id`, `status` (`deployed` or `failed`), and
timezone-aware `completed_at` and `recorded_at`. The latter is the durable
recording time. Pending and no-change dispositions also bind to the
request and generation and carry no deployment attempt ID or count. Request replay and multiple receipts referring to a single terminal
attempt count once.

## Optional web proxy recovery

Public collector fetches can use `HANSET_WEB_PROXY`, a loopback SOCKS5 URL.
Collect-web, scheduled active, and seed discovery use bounded preflight probes.
A final `proxy_unavailable` or `proxy_exit_mismatch` switches the run to the box
IP. `proxy_invalid` and `proxy_check_refused` stop collection.

A confirmed outage before response headers permits one direct attempt. If a
body stream is interrupted by a confirmed proxy outage, the collector discards
the partial body, records an error, and uses the box IP from the next URL. HTTP
403, captchas, login walls, paywalls, and other site refusals are never replayed
on another egress. Domain stops and robots cache entries carry over. Instagram
session handling and provider cost accounting are tracked in separate work items.

After an initial failed probe, an unset or blank
`HANSET_WEB_PROXY_HEAL_CMD` causes two additional probes, after successive
2- and 4-second waits. With a nonblank command, the collector attempts that
command once, then makes at most three additional probes, after successive
15-, 30- and 45-second waits. A healthy probe ends the retries immediately.
An initially healthy proxy runs no command and incurs no retry wait.

The optional command is split with `shlex.split()` and executed without a
shell, with a 30-second timeout and stdin/stdout/stderr discarded. Shell
operators, variable expansion and pipelines are not interpreted. Configure
an executable or an interpreter plus script arguments in the external runtime
environment. Parsing errors, launch failures, timeout and nonzero exit remain
quiet and still lead to the bounded probes. A successful probe restores proxy
egress. Command arguments, environment
values, subprocess output and exception text are never included in diagnostics.

The optional example is `bash scripts/proxy_heal_example.sh 1080` when invoked
from the repository root; substitute the actual SOCKS port. An externally set
`HANSET_WEB_PROXY_HEAL_CMD` can name that command. The script requires Linux
pidfd support in both Python and the kernel. It inspects process metadata
without printing command lines, matches the current user's actual `ssh`
executable with `-N` and an exact `-D 127.0.0.1:<port>` argument (including
attached short-option forms), and sends TERM only to a unique match.
It pins the process with a pidfd and checks ownership, start ticks, executable
identity and arguments again immediately before signaling. The existing
external keep-alive loop performs reconnection. The script never terminates
that parent or escalates to KILL. Exit codes are 0 for TERM sent, 2 for an
invalid port, 3 for no match, 4 for ambiguous matches, 5 for metadata/identity,
permission or signaling failure, and 6 for unavailable Python pidfd APIs.

Each web invocation starts with these per-run diagnostics in its result and
`status.json`, also returned by `collect status`:

```json
{"proxy_heal":{"attempted":false,"heal_exit":null,"retries":0,"recovered":false}}
```

`attempted` becomes true when the configured hook is attempted after a failed
initial probe, including parsing or launch failure. `heal_exit` is the actual
integer return code when available, otherwise null (including timeout).
`retries` counts additional probes actually made, excluding the initial probe,
from 0 through 3. `recovered` means a failed initial probe was followed by a
successful probe, even without a hook; it does not imply collection succeeded.
The values survive later successful, stopped, deferred and partial results.
These diagnostics never enter `checkpoint.json`. Older status files remain
readable, and Instagram results do not acquire this field. Public status only
accepts the exact four fields with their declared types.

The collector logs one `proxy_fallback_direct` event per fallback. Public
`web_egress` has exactly four fields. The `event` is `proxy_fallback_direct`,
`mode` is `direct`, `reason` is `proxy_unavailable` or `proxy_exit_mismatch`, and
`phase` is `preflight` or `mid_run`. Seed discovery returns this object as
`egress`.

Preflight preserves its existing detection limit: without
`HANSET_WEB_PROXY_EXIT_IP`, it checks only the TCP listener. Detecting a
listening but hung SOCKS tunnel requires that expected exit IP to be configured,
which enables a proxied check using `HANSET_WEB_PROXY_CHECK_URL` (default
`https://api.ipify.org`). Retries cannot detect a proxy the probe reports as
healthy. See the [two-hour operations schedule](RUNBOOK.md#continuous-seed-discovery-and-collection).

Recovery tests mock probes, subprocesses and signals and use a fake clock:

```sh
python3 -m pytest tests/active_bot/test_web_proxy.py tests/active_bot/test_proxy_heal_example.py -q
bash -n scripts/proxy_heal_example.sh
```

## Desktop Instagram handoff

Automatic profile reads, follows, and posts are not implemented. The desktop
operator does all signed-in Instagram work:

1. Only between 13:00 and 14:00 Asia/Seoul.
2. Only official venue accounts, 10 to 20 reads that day.
3. Follows, if any, stay a manual desktop action at about 5 and never more
   than 10, with gaps. This collector has no follow or post command.
4. On a login, checkpoint, captcha, or security warning, stop immediately. Do
   not solve the challenge. Write `/workspace/hanset-data/ig/inbox/security_halt.json`
   as `{"halt": true, "reason": "login_warning"}` and do not write captions.
   The collector latches that KST day and deletes the flag. The next day is
   eligible again. There is no account rotation or session repair.
5. Otherwise write one batch, `/workspace/hanset-data/ig/inbox/batch.jsonl`,
   mode `0600`. No photos, screenshots, captions, or media fields. One JSON
   object per line, at most 20 lines. `operator_summary` is the operator's
   own short paraphrase of what they saw, at most 480 characters. It is not
   the caption and it must not be copied from the post. `provenance` must be
   the exact string `operator_summary`. A `text` field is rejected so a
   caption cannot be stored as a quote:

```json
{"venue_id":"example-venue","venue_key":"example-venue","account_handle":"example.venue","post_url":"https://www.instagram.com/p/ExamplePost/","operator_summary":"SAMPLE operator summary, not an original caption. Weekday 09:00-18:00.","provenance":"operator_summary","seen_at":"2026-11-08T13:10:00+09:00"}
```

`seen_at` must be the same KST date, during hour 13, and not more than five
minutes ahead of the ingest clock. `post_url` must be a canonical HTTPS
profile, `/p/`, or `/reel/` URL with no query or fragment. A profile URL must
be that watchlist handle. A post or reel URL does not contain the handle, so
the collector cannot prove the post belongs to the account: the desktop
operator must, and the batch's `account_handle` must still match the
watchlist. The watchlist entry must match `venue_id`, `venue_key`, and
`handle`. One enabled handle cannot be attached to two venues. There is no
`follow_allowed` switch. Missing or any other `provenance` value stores
nothing and does not call the model.

Then, still inside the hour, with the key loaded the same way as web:

```sh
bash /workspace/hanset-site/scripts/run_collect_cron.sh ingest
```

`ingest` refuses work outside hour 13 before it reads the batch. A security
phrase in the batch halts the day, deletes the batch, and stores no claim.
A batch that is processed, including one that stores no claim, is deleted.
Model failure keeps the batch so the operator can retry inside the same
window. A lock conflict does not read or delete it. Either way the summary
stays only in the inbox. The database keeps a grounded span of that summary,
not the rest of the line. A batch whose line count exceeds the remaining
daily read budget (20 lines, counting lines already stored) is `ig_quota`:
the file is left for the operator and its summary is not copied into status,
checkpoint, or claims. `ig_reads` below 10 is `below_target` and is still
success when the batch itself was processed. The desktop, not this program,
is responsible for stopping at 20 reads and for not following or posting.

Instagram claims remain source claims attached to the post URL. They are not
verified facts and are not published. Two weekday observations that name
different hours are both kept. The report lists that conflict and does not
choose a current value or delete either row. The same summary for the same
URL is a duplicate and does not change the stored provenance.

## Provenance migration

The next writable open of `collector.sqlite3` adds nullable columns `evidence_origin`,
`evidence_role`, and `verbatim_source`. The migration does not UPDATE
existing rows. A row written before this change, including the Ambassador
hours source claim already stored by the live web run, stays NULL. Read it
as unrecorded. Do not relabel it with SQL. New web inserts are
`web_verbatim` / `page_span` / `1`. New Instagram inserts are
`operator_summary` / `summary_span` / `0`. A new insert without that pair is
rejected. Databases created after this change also have a table check for the
same pairs.

This command opens the database read-only and prints claims and conflicts.
It adapts to schemas without provenance columns without applying migrations.
It runs offline:

```sh
HANSET_IG_DIR=/workspace/hanset-data/ig python3 -m hanset_active collect report
```

Manual and scheduled web collection reassess committed claims under the
collector lock, including after a partial run or when all web work has already
completed. The default policy is `HANSET_IG_DIR/promotion_policy.json`, with an
optional override through `HANSET_COLLECT_PROMOTION_POLICY`. Approved official
evidence or agreement between two approved independent sources can qualify an
internal fact candidate. Unqualified source claims show `미확인`. Site
publication still requires official-source confirmation.
Event dates await the operator-managed event and edition registry.
[Source claim promotion](SOURCE_CLAIM_PROMOTION.md) · [Event registry issue](https://github.com/SUNBURN-Golden/hanset-site/issues/129)

Operations replaces the previous daily web trigger with the two-hour web
trigger. Instagram ingestion follows the hour-13 desktop summary workflow.
[Schedule and commands](RUNBOOK.md#continuous-seed-discovery-and-collection)

## Checks

```sh
python3 -m pytest tests/active_bot tests/twin -q
bash -n scripts/run_collect_cron.sh
python3 site/build_site.py --release
```

## Gap-priority plan

The web trigger prepares a frozen daily gap-priority plan and a maximum-20
operator IG read list. See [GAP_PRIORITY.md](GAP_PRIORITY.md) for `collect plan-gaps`, scoring,
read-only evidence rules, replay/fallback behavior and manual watchlist proposal
application. Planning and proposals never publish or change the live watchlist.

## Official web change review feeds

Daily official web observations now feed offline hours/price/closure change
review. Detection preserves claims and emits proposals, rebuild candidates and
a VIRAL topic file without publishing or sending. See
[OFFICIAL_WEB_CHANGES.md](OFFICIAL_WEB_CHANGES.md) for commands, authorization,
bootstrap/rebaseline, artifact hashes and human review requirements. The
review runs through the web wrapper.
