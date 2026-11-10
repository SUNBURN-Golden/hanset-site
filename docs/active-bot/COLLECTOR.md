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

DeepSeek is the existing account identified by `DEEPSEEK_API_KEY`. This
change adds no new paid vendor. Tavily, when a target asks for it, remains
the existing 30-request daily cap. Its secret stays on the host.

The model default is `deepseek-flash` (DeepSeek's documented Flash id).
`HANSET_DEEPSEEK_MODEL=deepseek-v4-pro` is an optional same-account override
and costs more. Other ids, including retired aliases, fail as
`model_config_invalid`. Requests disable thinking mode and cap each response
at 1,200 output tokens. The persistent budget is 36 calls, 180,000 input
characters, and 32,000 output tokens per KST day. A reserved call stays spent
if the process stops. The next KST day starts a new budget. Rolling the clock
backward fails closed as `state_corrupt`.

Web fetches use the existing active-bot fetcher: honest User-Agent, robots,
denied hosts, redirect checks, and the active daily body/aux caps. This
collector adds its own ceiling of 10 pages per run and 20 pages per KST day.
It holds the active `run.lock` only during fetches. `active_busy` means the
active collector already held that lock; nothing was fetched. Official URLs
must be HTTPS except loopback fixtures in tests. A redirect that leaves the
approved domain is `off_domain` and is not sent to the model. Asset and policy
URLs are not sent either.

Tavily is optional. A target with no URL and a `tavily` venue/field pair may
call the existing `discover_known_venue` hook, which already caps basic search
at 30 requests per KST day. Only a lead whose domain is in that target's
`official_domains` can be fetched, and at most one. Other leads are counted
and discarded. A missing Tavily key skips that target. The key file remains
`/workspace/.secrets/tavily_key` and is not read by this wrapper.

## Layout

| Path | Role |
| --- | --- |
| `/workspace/hanset-data/ig` | Checkpoint, status, claims DB, inbox, logs. Mode `0700`. |
| `/workspace/hanset-data/ig/deepseek.env` | Optional `0600` file owned by the runtime user. |
| `/workspace/hanset-data/ig/web_targets.json` | Approved official URLs. Not the example in git. |
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

`official_urls` is the supported activation path. `tavily` is optional and
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

The next open of `collector.sqlite3` adds nullable columns `evidence_origin`,
`evidence_role`, and `verbatim_source`. The migration does not UPDATE
existing rows. A row written before this change, including the Ambassador
hours source claim already stored by the live web run, stays NULL. Read it
as unrecorded. Do not relabel it with SQL. New web inserts are
`web_verbatim` / `page_span` / `1`. New Instagram inserts are
`operator_summary` / `summary_span` / `0`. A new insert without that pair is
rejected. Databases created after this change also have a table check for the
same pairs.

This command opens the database, applies that column add, and prints claims
and conflicts. It does not call DeepSeek, read Instagram, or change the web
cron. Run it while the 04:15 web job is not in the middle of a write:

```sh
HANSET_IG_DIR=/workspace/hanset-data/ig python3 -m hanset_active collect report
```

The operations host already runs web collection daily at 04:15 KST. Do not
register another cron for this change. Instagram ingest is still only
`scripts/run_collect_cron.sh ingest` during hour 13, and only after a
desktop summary batch is written. This repository does not install that line.

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
