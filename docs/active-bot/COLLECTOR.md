# DeepSeek web collector and desktop Instagram handoff

This collector writes internal `source_claim` rows under `HANSET_IG_DIR`
(default `/workspace/hanset-data/ig`). It does not publish the site, fill active
gaps, or write the twin. No site output change means no deploy.

The retired session-file Playwright daemon is not implemented. Do not add a
browser profile, cookies, CDP, proxy, or login-challenge bypass. Signed-in
Instagram use stays on the approved desktop. This repository only ingests the
desktop's operator-summary batch.

`hanset_active.connectors.instagram.InstagramConnector` stays a gate that
performs no network or browser work. HTTP collection still refuses Instagram
and Naver. `runport` / `런포트` / `butfit` / `버핏` are excluded.

## What a successful run means

`success: true` means this invocation finished the work it was allowed to do.
`collected: true` means at least one new grounded claim was stored. A process
that starts, locks, and exits is not by itself a collection. `no_targets`,
`missing_credentials`, `outside_window`, and `no_batch` are not collections.

Claims are `status=source_claim` and `rights=internal_review_only`. Web claims
use `source_type=own_site`. Instagram claims use `source_type=sns`. Neither is
a verified fact and neither is published. Quotes are at most 240 characters.
Raw page text, captions, and photos are not stored. Model reasoning text is
discarded. A new web quote is a page span (`web_verbatim`). A new Instagram
quote is a span of the operator's paraphrase (`operator_summary`), not the
post's original caption. Grounding that span checks the paraphrase only. It
does not verify the post. Rows stored before provenance columns existed keep
NULL origin and are unrecorded, not relabeled.

DeepSeek is the existing account identified by `DEEPSEEK_API_KEY`. The
owner-approved Qwen backup tier (below) uses a host-provided token-plan key.
Tavily, when a target asks for it, remains the existing 30-request daily cap.
Its secret stays on the host.

The model default is `deepseek-flash` (DeepSeek's documented Flash id).
`HANSET_DEEPSEEK_MODEL=deepseek-v4-pro` is an optional same-account override
and costs more. Other ids, including retired aliases, fail as
`model_config_invalid`. Requests disable thinking mode and cap each response
at 1,200 output tokens. The persistent budget is 36 calls, 180,000 input
characters, and 32,000 output tokens per KST day. A reserved call stays spent
if the process stops. The next KST day starts a new budget. Rolling the clock
backward fails closed as `state_corrupt`.

## Qwen backup tier (owner-approved 2026-11-11)

Every model step (web extraction, Instagram summary extraction, gap ranking)
retries a failed DeepSeek call exactly once via Qwen4-Max. The retry fires
only for `model_unavailable`, `model_rate_limited`, `model_transport`,
`model_timeout`, `model_auth`, and `model_insufficient_balance` — which covers
402 credit exhaustion, 429 limits, and 5xx. Invalid model output, an exhausted
budget, and missing DeepSeek credentials (`missing_credentials`) never fall back.
After a primary 402, that client skips DeepSeek for its remaining calls and
goes straight to Qwen. The first primary reservation stays spent; skipped
attempts make no request and reserve no budget. Extraction reuses one client
across batches; ranking uses a separate client. A new client tries DeepSeek
again. A Qwen 402 does not activate the primary skip.

- Endpoint: `https://token-plan.maas.qwencloudapi.com/compatible-mode/v1/chat/completions`,
  model `qwen4-max-preview`, same bounded messages as the primary attempt.
  The Qwen request sets `enable_thinking: false`, omits the DeepSeek-only
  `thinking` parameter and `response_format`, and uses a 60 s read timeout
  instead of 20 s. A single surrounding Markdown `json` fence is accepted for
  Qwen only; the content must still parse as a JSON object. Empty content,
  including reasoning-only replies, is `model_invalid`; reasoning is discarded.
- Key: read at runtime from `/home/box/.qwen/token_plan_key`, overridable
  with `HANSET_QWEN_KEY_FILE`. A missing, unreadable, symlinked, oversized,
  or repo-internal key file silently skips the backup and keeps the original
  error. The key is cached in process memory only; a new run picks up
  rotation. It is redacted like other secrets and never logged, persisted,
  or committed. No wrapper or cron change: the file is read at runtime, not
  sourced.
- If Qwen also fails (credit exhausted, 429, limit, timeout, invalid output),
  the original DeepSeek reason is raised and existing retry/fallback
  handling proceeds. There is no third tier in the collector.
- Both attempts charge the same daily budget via `reserve_model`; the failed
  primary reservation stays spent, so the caps cannot be exceeded by the
  retry. With no headroom for the retry, the original error is kept.
- `provider` in run results, `status.json`, and `collect status` counts which
  provider answered successfully (`deepseek` / `qwen`). It is per-run
  diagnostics: not part of `checkpoint.json`, and plan artifacts keep the
  configured DeepSeek id in `ranking.model` (see GAP_PRIORITY.md).
- Run results, `status.json`, `collect status`, and planning results also report
  `primary_skipped: "insufficient_balance"` when a primary 402 occurred, even if
  Qwen fails. This diagnostic is absent otherwise, is never checkpointed, and
  never controls routing on a later run. Successful fallback retains its ordinary
  success reason; failed fallback retains `model_insufficient_balance`.
- Model calls (DeepSeek and Qwen) go direct with `trust_env=False` and never
  use the SOCKS web proxy.

The 2026-11-15 live probe confirmed that disabling Qwen thinking produces JSON
content within the output cap. Adding `response_format` produced malformed
output, so it remains omitted. Tests never make live model calls.

Web fetches use the existing active-bot fetcher: honest User-Agent, robots,
denied hosts, redirect checks, and the active daily body/aux caps. This
collector adds its own ceiling of 10 pages per run and 20 pages per KST day.
It holds the active `run.lock` only during fetches. `active_busy` means the
active collector already held that lock. Web targets require HTTPS except for
loopback fixtures. The fixed User-Agent includes `hanset@soulbound.co.kr` and
requests are serialized per registrable domain. Each redirect must pass the
URL guards and stay within the starting registrable domain before the collector
contacts the destination. Instagram is outside web collection. Asset and policy
pages do not reach the model.

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
slash variants and drive queue, checkpoint, and cooldown comparisons. Each
identity keeps its maximum score and first validated fetch URL, preserving the
original encoded path and trailing slash for the actual request.

Fresh children run immediately after their parent, ahead of new roots. All
children use the same fetcher, proxy, robots rules, locks and budgets. Children
never recurse. A child under a block hold is noted as skipped without consuming
one of the two subpage slots. Redirects with unsafe URL identities, including
session parameters, are refused before the destination is contacted.
Every non-`cap_reached` fetch attempt counts toward the existing 10-page run and
20-page daily caps, including robots refusals and collateral domain stops. Link
selection precedes extraction and receipt deduplication, so unchanged or
empty-extraction roots can still yield useful children. Unprocessed children
remain in memory and must be rediscovered on a later eligible root fetch.
Only newly discovered children enable the fetcher's subpage mode: paths stay
intact through normalization, with at most two HTTPS redirects within the
initial registrable domain. External redirects and downgrades are rejected
before destination robots or body requests. Every permitted hop still passes
URL denial checks and robots evaluation against its preserved path; a robots
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
clock. `robots_disallowed` always establishes a hold. `login_wall` and `blocked`
establish holds only when a real response status exists, including security text
on a 200 response. A later URL stopped collaterally by the fetcher's in-run domain
stop has `blocked` with no status: it still counts this run, but gets no hold and
can be retried next run. A blocked homepage therefore does not create a cooldown
for an independently approved venue subpage.

Holds last 14 days per URL. `recent_block_skipped` makes no fetcher call, consumes
no page slot, and is never added to checkpoint `completed`. The checkpoint is
shared with same-day Instagram ingestion: completed keys that fail the safe-URL
check (such as denied `instagram.com` post URLs) stay inert for web deduplication
and never abort a web run. Skips do not refresh the timestamp. Exactly at expiry the URL is eligible; a new genuine terminal
result starts a fresh hold. Expired entries are pruned when opening cooldown
state and in each write transaction; malformed entries fail as `state_corrupt`.
Skip notes follow fetch/extraction notes so the 20-entry public status limit
preserves real results; `rejections.recent_block_skipped` reports the total.
When no unfinished URL remains outside cooldown, the result is successful with
`collected: false`, reason `recent_block_skipped`, before extraction credential
checks or proxy preflight.

Search leads are only scheme/domain-checked upstream. A lead whose identity fails
the safe-URL check (credential params such as `?sid=`/`?key=`, redaction markers,
over-length) skips that one job with an `unsafe_url_skipped` note and a
`rejections.unsafe_url_skipped` count; it is never fetched and its raw URL is not
echoed into status. Other jobs in the same run proceed normally. If every job is
unsafe, the run stops with reason `unsafe_url_skipped` before credential checks
or proxy preflight.

Until the Korean-news robots helper from COLLX-01c is available, web collection
uses the existing robots checks and spacing on all hosts. A robots refusal on
a shared platform such as Naver can stop other targets on that registrable
domain for the current run. Only an evaluated URL gets the 14-day hold.
Robots transport errors and 5xx responses normally count as `robots_disallowed`.
A confirmed proxy outage is recorded as a fetch error and leaves the robots
cache and block holds alone.

Tavily is optional. A target with no explicit official URL and a `tavily` venue/field pair may
call the existing `discover_known_venue` hook, which already caps basic search
at 30 requests per KST day. Only a lead whose domain is in that target's
`official_domains` can be fetched, and at most one. Other leads are counted
and discarded. A missing Tavily key skips that target. The key file remains
`/workspace/.secrets/tavily_key` and is not read by this wrapper.

### Seven-day soft deferral of unchanged pages

After URL safety, block holds, and same-day completion checks, web execution
uses observations from the past seven days to defer canonical requested URLs
with `duplicate`, `empty_extraction`, or `applied` with a hash matching an
applied web receipt for that URL. At seven days the URL returns to normal
priority. Initial jobs keep their planner order within each queue. The normal
queue drains first and deferred jobs fetch with spare capacity under the same
caps and guards.

Fresh link candidates get first choice of each venue's two subpage slots.
Within each group the original link ranking applies. Fresh children follow
their parent immediately, and deferred children wait behind all normal jobs,
including manual and gap-derived targets. A promoted explicit child retains its
request URL, authorization, and default fetch behavior. An explicit child
already in the deferred queue keeps its place there.

The external SQLite table `web_target_observations` stores the latest fetch
timestamp, disposition, and available content hash under the SHA-256 digest of
the safely canonicalized requested URL. A ready page records
`pending_extraction` at fetch time. A completed extraction records `applied`,
`duplicate`, or `empty_extraction` with the same timestamp. Both duplicate
paths and discovered children update this history. A failed fetch or unfinished
extraction gives the URL normal priority when it is eligible to retry.

Until a URL has scheduling history, the collector uses its latest safe web
receipt or change observation. A change observation can extend the timestamp
when its canonical URL and hash match a receipt. Receipt creation time is all
that is available for historical repeats without a recorded observation.
Scheduling entries stay available after the seven-day window, and existing
evidence rows retain their original timestamps and labels.

`rejections.recent_unchanged_deferred` reports the number of distinct canonical
URLs admitted to the deferred queue, including jobs fetched with spare capacity.
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
| `/workspace/hanset-data/active` | Existing fetcher budgets and robots cache. |
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

Default web runs derive venue-owned URLs from open/conflict priority gaps and
public URLs from the active seed freeze in a read-only DB snapshot. The next
operator-initiated `seed --refreeze` can admit Naver and declined brands such as
RUNPORT. Existing freeze rows stay in place. Manual targets take precedence,
disabled identity selectors suppress derivation, and invalid files block
derivation. Missing or empty manual files permit derivation.

Each target has a fetch list (`urls`), a redirect scope (`scope_domains`), and
an official subset (`official_urls` and `domains`). Public blog, portal, map,
OTA and declined-brand seed pages can be fetched. They grant no official
authority and supply no follow-up links. A canonical URL shared by a seed and
a manual target is attributed to the manual target. Seed collection does not
create venue listings. The 100-target and 10-page run / 20-page daily limits
still apply. A plan that exceeds its 512 KB file bound fails as `plan_invalid`.
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

That is the web run. It loads `deepseek.env` when the file is present, takes
`cron.lock`, and runs `python3 -m hanset_active collect web` for at most 25
minutes. A second overlapping run logs `already_running` and exits 0.
`collect status` prints the last public status and does not collect.

Exit 0 is only `done`, `already_complete`, `already_running`, or idle status.
`done` with `collected: false` finished without a new claim (robots refusal or
an empty extraction, for example). Exit 2 means the run did not finish that
way: missing key, invalid targets, shared-lock deferral, model error, or cap.

The cron line is not installed by this change. After review, an operator on a
host whose cron timezone is Asia/Seoul can add:

```cron
15 4 * * * HANSET_IG_DIR=/workspace/hanset-data/ig HANSET_ACTIVE_DIR=/workspace/hanset-data/active HANSET_TWIN_DIR=/workspace/hanset-data/twin /workspace/hanset-site/scripts/run_collect_cron.sh
```

The variables name existing directories. They are not secret. Do not put
`DEEPSEEK_API_KEY` in the crontab. Confirm the host timezone before enabling
the line. Web collection is not restricted to 13:00 KST. If the active
collector is inside its own run, this job returns `active_busy` and can be
repeated later. It does not start Instagram.

Direct inspection without the wrapper, when the key is already in the
environment:

```sh
python3 -m hanset_active collect web
python3 -m hanset_active collect status
```

## Optional web proxy recovery

Only collect-web fetches use `HANSET_WEB_PROXY`, a loopback SOCKS5 URL.
When configured, preflight runs under the collector process lock, after
planning/search and before the active fetch lock and `Fetcher` construction.
Every retry probes the same configured proxy. If the final probe reports
`proxy_unavailable`, the run fetches directly. A wrong exit IP stops the run
with `proxy_exit_mismatch`. Invalid proxy configuration stops it with
`proxy_invalid`.

A mid-run connection error switches subsequent jobs to direct egress only if
the proxy is unreachable. The failed URL consumes its attempt and receives an
`error` observation. It can be retried on a subsequent run. Domain stops,
budgets, and robots cache entries carry over to direct egress. Site refusals
leave routing alone. DeepSeek, Qwen, Tavily and Instagram use their existing
routing.

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

Direct fallback adds `web_egress` with `mode: direct`, `reason: proxy_unavailable`
and `phase: preflight` or `mid_run` to the result and public status.

Preflight preserves its existing detection limit: without
`HANSET_WEB_PROXY_EXIT_IP`, it checks only the TCP listener. Detecting a
listening but hung SOCKS tunnel requires that expected exit IP to be configured,
which enables a proxied check using `HANSET_WEB_PROXY_CHECK_URL` (default
`https://api.ipify.org`). Retries cannot detect a proxy the probe reports as
healthy. The existing daily 04:15 KST web schedule remains unchanged; this
feature installs no scheduler, tunnel supervisor or deployment step.

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
It does not call DeepSeek, read Instagram, or change the web cron:

```sh
HANSET_IG_DIR=/workspace/hanset-data/ig python3 -m hanset_active collect report
```

The explicit `collect promote --policy /outside-checkout/policy.json` review
gate can create internal fact candidates from two agreeing independent source
approvals. `review_only: true` admits third-party web evidence to this gate.
Third-party claims and candidates supported entirely by third-party claims
show `미확인`. Public facts require official-source confirmation.
`--dry-run` is read-only; `collect report --policy ...` reassesses without
persisting. Both retain conflicts and never publish. See
[SOURCE_CLAIM_PROMOTION.md](SOURCE_CLAIM_PROMOTION.md) for source approvals,
normalization, candidate migration, evidence holds and observation-date markers.

The operations host already runs web collection daily at 04:15 KST. Do not
register another cron for this change. Instagram ingest is still only
`scripts/run_collect_cron.sh ingest` during hour 13, and only after a
desktop summary batch is written. This repository does not install that line.

Before the first 04:15 KST run using this scheduling change, capture the
available run history for the prior seven days. Compare the new run's fetched
canonical requested URLs with that snapshot. The target is at most two
`duplicate` dispositions and at least three fetched URLs absent from the
seven-day history. Report the new-URL count as unverified if historical
coverage is incomplete.

## Checks

```sh
python3 -m pytest tests/active_bot tests/twin -q
bash -n scripts/run_collect_cron.sh
python3 site/build_site.py --release
```

Web collection is already live on the operations host, including the 04:15
KST cron and a stored Ambassador hours source claim. This change does not
rerun that path and does not register a cron. Still unverified on the host:
opening the live database with `collect report` (the Ambassador row should
stay NULL), a real desktop operator-summary batch during hour 13, and any
model extraction of that summary. Tests mock DeepSeek. Do not treat a green
test run as that live ingest. Do not widen the Instagram hour to try it.

## Gap-priority plan

The existing 04:15 KST web trigger now prepares a frozen daily gap-priority plan
and a maximum-20 operator IG read list. No cron changes or Instagram browser
are added. See [GAP_PRIORITY.md](GAP_PRIORITY.md) for `collect plan-gaps`, scoring,
read-only evidence rules, replay/fallback behavior and manual watchlist proposal
application. Planning and proposals never publish or change the live watchlist.

## Official web change review feeds

Daily official web observations now feed offline hours/price/closure change
review. Detection preserves claims and emits proposals, rebuild candidates and
a VIRAL topic file without publishing or sending. See
[OFFICIAL_WEB_CHANGES.md](OFFICIAL_WEB_CHANGES.md) for commands, authorization,
bootstrap/rebaseline, artifact hashes and human review requirements. The
existing web wrapper and schedule are unchanged.
