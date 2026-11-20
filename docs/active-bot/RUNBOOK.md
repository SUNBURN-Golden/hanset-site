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

The wrapper `scripts/run_active_cron.sh` (executable) finds its checkout, validates the external data directory,
uses a cron flock in addition to the collector's own flock/DB lease, and
appends each output line with a KST timestamp. For example, schedule it at
minutes 0 and 30 in a local crontab using the absolute checkout path:

```cron
0,30 * * * * HANSET_ACTIVE_DIR=/workspace/hanset-data/active /workspace/hanset-site/scripts/run_active_cron.sh
```

An overlapping invocation reports `already_running` and exits successfully.
SIGTERM completes the current task then stops. SIGKILL requires no manual
queue edits: the next exclusive run recovers orphaned tasks. Completed
subrequests have checkpoints; an incomplete evidence stage is safely replayed.
Failures get at most three attempts, with backoff of 10 minutes and one hour;
the final dead record retains the six-hour next timestamp for inspection.
Blocks, login walls, and robots denial are terminal and are never bypassed.
A 429 records Retry-After and stops that domain for the run.

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

Operations owns scheduler installation. In the KST crontab, install the
discovery trigger and replace the prior 04:15 web line with the web trigger.

```cron
05 */2 * * * /path/to/checkout/scripts/run_seed_discovery_cron.sh
35 */2 * * * /path/to/checkout/scripts/run_collect_cron.sh web
```

The discovery wrapper records results under `HANSET_ACTIVE_DIR/logs`. Incomplete
license and sitemap passes resume at the next invocation. Instagram intake
remains an operator summary plus the post URL. [Web collector](COLLECTOR.md)

## Offline validation

```sh
python3 -m pytest tests/active_bot -q
```

Tests use loopback HTTP and fake clocks. PDF/OCR tests skip when their system
binary is unavailable; the crash test kills a test subprocess after a persisted
raw insert. The legacy `run` queue retains its production spacing. Discovery and web
collection apply robots and crawl spacing to Korean news hosts. All three
entry points use the per-domain request lock.
