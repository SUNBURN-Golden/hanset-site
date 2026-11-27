# Active collector operations

Run from the checkout with Python 3.11 or later. Dependencies are requests,
beautifulsoup4, lxml, Pillow, and pytest for tests. Install `pdftotext` and
`tesseract` with English/Korean language data for attachment extraction.
Playwright is optional and imported only for a permitted empty app shell.
No model, credential, or authenticated session is needed for normal collection.

## Data directory

Set `HANSET_ACTIVE_DIR` to an external directory writable by the collector.
The default is `/workspace/hanset-data/active/`. SQLite (`active.sqlite3`),
sanitized raw text (`raw/`), lock files, and cron logs (`logs/`) live there.
A path inside the checkout, including a symlink into it, is rejected. Back up
SQLite with SQLite's backup API, together with `raw/`; do not copy a live WAL
file independently. No collected data is published to the service site.

`hanset_active init` is the explicit bootstrap for `active.sqlite3`.
Manual web targets can run during an authorization database outage. The web
collector uses `collect-fetch.sqlite3` for fallback request audits and shared
host policy, and `collect-leases.sqlite3` for its run lease. Scheduled active
and seed jobs defer until the authorization snapshot can be read.

## Commands

```sh
export HANSET_ACTIVE_DIR=/workspace/hanset-data/active
python3 -m hanset_active init
python3 -m hanset_active seed --csv /external/seeds/saunas.csv --csv /external/seeds/boxes.csv
python3 -m hanset_active run --max-minutes 25 --max-requests 100
python3 -m hanset_active status
python3 -m hanset_active report --json
python3 -m hanset_active gaps --venue 'Example Sauna'
python3 -m hanset_active tasks
python3 -m hanset_active failures
python3 -m hanset_active export-service-data --out /external/active-export
python3 -m hanset_active ig-stub-check
```

The seed freezes known identity; repeating it does not update that snapshot.
`seed --refreeze` explicitly creates a new snapshot. `--approved-sources PATH`
can select an optional approved-origin input. Without `--csv`, seed reads the
checkout's root seed files if present. Those inputs are read-only.

A smaller discovery profile can be selected with global `--profile PATH`
(before the subcommand). `--data-dir PATH` overrides the environment similarly.
`run --classes backfill,discover,freshness` selects queue classes.
`run --dry-run` lists eligible queued work without fetching or bootstrapping.
Run limits can lower consumption; persisted daily caps remain enforced.

## Cron

Run these from the production checkout after merge. Use the operations
account that owns the collector crontab.

If the seed schedule is missing, register it once with the seed-only installer.
It works before publication readiness and reconciles only the tagged seed job
at :15 in odd-numbered KST hours. The preview checks the timezone of that entry.

```sh
scripts/install_collect_crons.sh --seed-only --dry-run
scripts/install_collect_crons.sh --seed-only
```

Use the full installer after the release consumer passes its readiness check.

```sh
scripts/install_collect_crons.sh --dry-run
scripts/install_collect_crons.sh
scripts/install_collect_crons.sh --uninstall --dry-run
scripts/install_collect_crons.sh --uninstall
```

The installer manages the jobs with the trailing `# hanset-collx` marker.
It adds seed discovery and wide collection at 01:15, 03:15, and every two
hours through 23:15 KST. The watchdog runs every 15 minutes. Keep the
existing 04:15 web collection, hourly :35 active, hourly :50 twin, and
hourly :05 Linux crawler entries. The installer preserves timezone
assignments and checks the daemon's timezone and the effective timezone
of the managed entries. If the daemon environment is unreadable, use
`HANSET_CRON_TIMEZONE=Asia/Seoul` only after independently verifying the
daemon's schedule timezone. Dry-run redacts removed command text and
reports unmet prerequisites without writing the table.

Operations confirms the actual release consumer with a non-publishing
contract check before enabling the wide schedule. The versioned readiness
file and the request and receipt formats are in the
[publication handoff runbook](COLLECTOR.md#operations-publication-handoff).
Without readiness, the full installer leaves the crontab intact and the
watchdog skips wide collection. The 04:15 entry keeps its default behavior. Operations
can enable publication there with `--daily-publish` on both the dry run and
install.

### Proxy settings for scheduled collection

The full installer copies literal `HANSET_WEB_PROXY` and `HANSET_WEB_PROXY_*`
assignments from the first operator-owned 04:15 web collection into the
managed wide and watchdog jobs. With `--daily-publish` enabled, an existing
managed daily job can supply the settings when there is no operator entry.
Publication opt-in and reversal apply to every eligible operator daily entry.
Keep inline proxy values literal. The installer rejects shell expansions and
commands that use cron stdin when they supply proxy settings. The dry-run
preview redacts the copied settings and file path.

For credential-bearing settings, including sensitive heal command arguments,
move all proxy settings off the 04:15 command before running the installer.
Cronie logs command text. Create a regular JSON file outside the checkout,
owned by the cron user with mode `0600`, and set the literal absolute path
in `HANSET_COLLX_PROXY_ENV_FILE` on the 04:15 command. The JSON object maps
`HANSET_WEB_PROXY` and `HANSET_WEB_PROXY_*` names to string values. For example,
the crontab can carry the file reference alone. The installer rejects a
file reference combined with any inline proxy assignment, including an
empty assignment or one supplied through `env`.

```cron
15 4 * * * HANSET_COLLX_PROXY_ENV_FILE=/private/ops/collect-proxy.json /workspace/hanset-site/scripts/run_collect_cron.sh web
```

Run the full installer to propagate the file reference. The watchdog validates
the file at startup and forwards its path to daily and wide catch-up wrappers.
Both scheduled and catch-up wrappers preserve the path while loading
`deepseek.env`. The supervisor then reads the current file, replaces all
inherited proxy settings with its contents, and removes the path from the
payload environment. Rotate values in the file for the next launch. Symlinks,
unsafe permissions, and invalid files stop dispatch with `proxy_env_file_invalid`.
Network failures follow the proxy preflight and direct fallback policy.

### Scheduler behavior

The supervisor writes the scheduled KST slot to
`/workspace/hanset-data/stamps/<job>.attempt` immediately before launching
each ordinary or catch-up payload. It records successful completion time
in `<job>.ok`. A launch error, payload failure, or kill after the attempt
write spends the slot. The next ordinary retry is at 04:15 the next day for
daily collection, at the next scheduled slot for wide collection, and at
the next hourly slot for active. Publication recovery uses the wide or
opted-in daily checkpoint independently of the attempt stamp. Contention and deferrals
before dispatch leave the slot eligible.

The scheduler checks each job's latest slot, including across midnight,
once the slot is more than ten minutes old. Shared collector catch-ups run
oldest eligible slot first. Seed discovery and twin proceed independently.
Ordinary active can yield to an unaccounted shared peer slot only when some
declared job already has an `.ok` or `.attempt` stamp. The peer slot must
fall after the previous active slot and at or before the current one. Each
peer slot can therefore delay ordinary active by at most one hourly run.
Catch-up active can still wait behind an oldest peer that cannot launch.
Wrapper logs remain in each job's external `logs/` directory.
Seed discovery arguments select a manual run, including `--max-calls 0`.
Desktop ingest uses the existing manual wrapper mode.

### Managed site publication

Run publication commands as the operations account in `/workspace/hanset-site`.
The one-time readiness check takes the exact static directory preserved from
the last successful upload. Use that artifact from the release record or
operations' release storage.

```sh
cd /workspace/hanset-site
scripts/auto_publish.sh --check-readiness --deployed-artifact "$CONFIRMED_RELEASE_DIR"
scripts/install_collect_crons.sh --dry-run
scripts/install_collect_crons.sh
```

Readiness deliberately exercises Outbox verification, artifact tampering,
projection, rule-only evidence, WAL backup retention and receipt accounting
in private temporary state. It records the confirmed baseline and writes
`publication/v1/readiness.json` under `/workspace/hanset-data`. A failed
re-check invalidates readiness and retains the prior baseline. Operations
can pass `--rebaseline-baseline` with a newly confirmed artifact after a
manual release.

#### External operations prerequisites

- Supply `$HANSET_IG_DIR/site_slug_map.json` with exact
  `venue_id -> {venue_key, site_slug}` identities. A mapped slug must identify
  one venue.
- Supply `$HANSET_DATA_DIR/publication/v1/consent.json` with
  `schema_version: 1` and a `venues` array. Each record needs `venue_id`,
  `venue_key`, `slugs`, `names`, `aliases`, `official_urls`, `decision`
  (`allowed` or `declined`) and ISO `decided_on`. Include the
  `runport-mapo` refusal dated 2026-10-19.
- Register `/workspace/fitness-hub/ops/deploy.sh` by writing
  `hanset-release-v1` to its sibling `deploy.sh.hanset-contract` once the
  script implements the contract below. `HANSET_DEPLOY_CMD` can point to an
  absolute executable path using the same marker convention.
- Route manual releases through the external
  `/workspace/fitness-hub/ops/deploy_locked.sh` launcher. It takes the same
  exclusive, nonblocking `$HANSET_DATA_DIR/publish/deploy.lock` and passes
  its open descriptor to the release command. `HANSET_DEPLOY_LAUNCHER`
  overrides the path checked during readiness.

The external deploy script must use `HANSET_SITE_ROOT` as the checkout and
`HANSET_RELEASE_PYTHON` as the Python executable when supplied. The managed
checkout is a private snapshot of the tracked files. Call
`scripts/release_contract.py` from that checkout with the selected Python:

1. Run `check-lock` before any release work. Keep the descriptor in
   `HANSET_PUBLISH_LOCK_FD` open in the child, with its path in
   `HANSET_PUBLISH_LOCK_FILE`.
2. Run the external script's pytest gate under an allowlisted environment
   with provider credentials, all `HANSET_PUBLISH_*` variables and
   `HANSET_RELEASE_DATE` removed.
3. Rebuild with `--release` into `site/dist` and run the existing leak
   check. Preserve `HANSET_RELEASE_DATE` for the final build so the calendar
   and fonts match the staged artifact.
4. For a managed release, run `started "$DIST"` immediately before the
   existing upload. It verifies the exact artifact against
   `HANSET_PUBLISH_EXPECTED` and records that upload may now start. Only
   after a successful upload run `confirmed`. These two actions also use
   `HANSET_PUBLISH_RELEASE_ID`, `HANSET_PUBLISH_UPLOAD_STARTED`, and
   `HANSET_PUBLISH_RECEIPT`. A failed contract command must abort the release.

The external command starts with a clean environment and temporary home.
Its own operations credential configuration supplies upload access. Keep
credentials out of command arguments, receipts and logs. The external scripts
and marker are maintained by operations outside this repository.

#### State and recovery

- `scripts/auto_publish.sh --drain` processes pending requests together.
  `--request <request_id>` processes one request. `--status` reports
  readiness, baseline and pending status. Collection can succeed even when
  publication fails.
- Before apply, the publisher takes SQLite backups under collector locks and
  verifies each copy. `$HANSET_DATA_DIR/backups/` retains the newest 14
  verified sets. The publisher reads databases without migrating data.
- Read KST gate outcomes in `$HANSET_DATA_DIR/publish/publish.log`.
  The Outbox stores request receipts and the change ledger. The live release
  baseline points to the confirmed artifact manifest and input hash.
  Unchanged applied data skips build, tests and deployment.
- `scripts/auto_publish.sh --recover` finishes an interruption with a
  durable confirmation or records a failure before upload. With only an
  upload-started marker, use `--reconcile-live "$LIVE_ARTIFACT_DIR"` after
  obtaining an exact retained copy of the served artifact from operations.
  The comparison must uniquely identify the candidate or previous artifact.
- A confirmed release with interrupted checkout write-back blocks further
  publication until `scripts/auto_publish.sh --reconcile-checkout` completes.
  It applies changes only when the files match the recorded before or
  after hashes. Review and resolve conflicting human edits before retrying.

### Daily collector metrics

```sh
tail -n 1 /workspace/hanset-data/metrics/collect-daily.jsonl
```

Each line represents one KST date and closes at 23:30. The watchdog and
scheduled jobs save inventory and shared Tavily usage observations while the
box is awake. The report uses the latest pre-cutoff observation and counts
events from midnight up to, but excluding, the cutoff.

| Counter | Meaning |
| --- | --- |
| `seeds_new` | Distinct continuous-discovery candidate IDs first seen in the reporting window |
| `seeds_total` | All persisted continuous-discovery candidate IDs across all statuses |
| `seed_domains_total`, `seed_venues_total` | Domain and venue rows belonging to the current `seed_freeze` cursor |
| `seed_tavily_calls` | Seed-tagged Tavily API dispatches in the window |
| `tavily_used` | Shared Tavily usage across consumers at the saved observation |
| `fact_candidates_total` | Persisted fact candidates, including holds and conflicts |
| `facts_total` | Internal facts that currently qualify under reviewed policy and current authorization |
| `unverified_total` | Current source claims outside those facts’ supporting evidence |
| `targets` | Distinct enabled venue identities |
| `pages_fetched` | Completed, successful, nonempty body transfers |
| `claims_new` | Distinct claims counted by receipt creation time |
| `provider_calls` | Model dispatches by provider |
| `deploys`, `deploy_failures` | Distinct successful or failed attempts completed and durably recorded before cutoff |

On wake, metrics closes earlier dates from saved aggregates before sampling
today. A full date asleep carries forward inventory totals and counts its
own timestamped events. A missing Tavily bucket is zero. A nonzero bucket
without a pre-cutoff observation leaves that date retryable. Unreadable
state can delay an affected date while later dates close independently.
A recovered older date may appear last in the file. Historical observations
without the extended inventory still close using the original fields, and
pending appends of either format recover in place. Each emitted line stays
byte-identical on subsequent runs. Dates before metrics initialization are
outside the report.

## Reading reports

`new_valid_sources` counts registered domains outside the active frozen seed;
`new_venues` counts identities absent by both normalized name and domain.
Examples include their URL and discovery provenance. Pagination, PDF, and OCR
counts describe stored successful work, not inferred coverage.

Filled gaps require explicit original-source facts and include evidence URLs.
`not_extracted` means a field cue or attachment needs extractor work;
`absent_in_source` requires documented multi-page/counterpart/attachment
coverage; `inaccessible` means all attempted source pages were blocked;
`conflict` requires review of different facts from distinct source URLs.
An open gap still lacks sufficient evidence or exhausted coverage. Third-party
claims do not fill gaps. Historical values are kept in `field_changes`.

`duplicate_check.total` must be zero. A positive value requires investigating
the reported table's natural keys before using exports. `structure_alerts`
and `needs_extractor_review` identify changed layouts or lost extraction;
do not interpret them as evidence that a field is absent. Provider states and
inaccessible reasons explain unavailable discovery paths. Request usage includes
robots/API requests, while daily bodies and auxiliary requests have separate
caps. AI cost is always zero; requests per fact is also reported.

The Instagram connector is a stub: disabled by default, with a gate requiring
explicit enablement, the KST hour 13, and a configured session-state path.
Even an open gate makes no network or browser call. Never add session material
to the checkout or logs. Session-file Playwright collection is not supported.

## Continuous seed discovery and collection

Run from the checkout using external active and Tavily state.

```sh
python3 -m hanset_active seeds discover
python3 -m hanset_active seeds discover --source licenses --source sitemaps
python3 -m hanset_active seeds discover --source tavily --max-calls 10
```

Sources are `licenses`, `directories`, `sitemaps` and `tavily`. The command
reports new, resolved, duplicate, rejected and promoted transitions overall and
by source. Overlapping discovery returns `already_running`. Collection has its
own run lock and can pick up committed targets between discovery pages, even
after its daily plan freezes.

License discovery reads the mode-`0600` file at `HANSET_DATAGO_KEY_FILE`, which
defaults to `/workspace/.secrets/datago_key`. A missing file reports
`skipped:no_key`. Each dataset completes one full pass and then replays an
inclusive update watermark. A failed page resumes at its saved cursor. Only
projected business identity and license fields are retained. Coordinates carry
their original EPSG:5174 CRS.

The shared Tavily ledger preserves KST day counters and records reported credit
usage without an internal daily cap. `--max-calls` bounds Tavily dispatches for
one invocation. Zero skips Tavily. Provider quota exhaustion stops Tavily for
the invocation and records `tavily_provider_quota`. Use the free plan with paid
usage disabled. Billing changes require 준태님's approval. [Tavily ledger](IG_DAEMON.md#persistent-usage-ledger)

The discovery wrapper records results under `HANSET_ACTIVE_DIR/logs`.
Incomplete license and sitemap passes resume at the next invocation.
Scheduled active, web collection, and seed discovery run bounded proxy preflight.
If the proxy is down or the exit IP mismatches, the run continues on the box IP
and logs one `proxy_fallback_direct` event. Invalid configuration
(`proxy_invalid`) and a refusal from the exit check (`proxy_check_refused`) stop
collection. The status object `web_egress` (`egress` for seed discovery) contains
`event`, `mode` (`direct`), `reason` (`proxy_unavailable` or
`proxy_exit_mismatch`), and `phase` (`preflight` or `mid_run`).
Instagram intake remains an operator summary plus the post URL.
[Scheduler installation](#cron) · [Web collector](COLLECTOR.md)

## Offline validation

```sh
python3 -m pytest tests/active_bot -q
```

Tests use loopback HTTP and fake clocks. PDF/OCR tests skip when their system
binary is unavailable; the crash test kills a test subprocess after a persisted
raw insert. Active entry points share an exact-hostname request lock. Robots
checks, Crawl-delay, and the five-second minimum interval apply to listed
Korean news hosts. Other hosts have no crawl delay.
