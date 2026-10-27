# Active collector operations

This is the draft for `docs/active-bot/RUNBOOK.md`. Installation at that path
requires resolving the task's file-scope restriction.

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

Install the reviewed wrapper as `scripts/run_active_cron.sh` and make it
executable. It finds its checkout, validates the external data directory,
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
to the checkout or logs.

## Offline validation

```sh
python3 -m pytest tests/active_bot -q
```

Tests use loopback HTTP and fake clocks. PDF/OCR tests skip when their system
binary is unavailable; the crash test kills a test subprocess after a persisted
raw insert. Production spacing is at least five seconds per domain and one
second globally, with one request in flight per domain.
