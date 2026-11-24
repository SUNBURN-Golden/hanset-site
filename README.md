# hanset-site

HANSET (한셋) is a Korean wellness and functional-fitness magazine and venue
finder. Production runs on Cloudflare Pages project `hanset` from `main`.
[Production site](https://hanset.soulbound.co.kr/)

Read the repository rules before making changes. [AGENTS.md](AGENTS.md)

The tracked handover notes record the history of the closed-days guide and
its build-time expiry behavior. [HANDOVER.md](HANDOVER.md)

## Static site

From the repository root, run `python3 site/build_site.py --release`. The
builder reads checked-in publication inputs and writes to `site/dist/` by
default. The HANSET operations bot owns publication.
[Build setup and local preview](site/README.md#build--run) · [Release route inventory](site/README.md#pages)

[Illustration workflow](illustrations/README.md) · [Data foundation design](docs/data/README.md)

## Site tests

From the repository root, use a Python 3.11+ test virtual environment with
`pytest` installed and Node.js on `PATH` for the JavaScript assertions. Keep
test and browser dependencies in test environments, separate from the site
build environment.

```bash
python3 -m pytest -q \
  tests/test_closed_month.py tests/test_detail_evidence.py \
  tests/test_drawing_disclaimer.py tests/test_drawing_hangul.py \
  tests/test_explorer.py tests/test_featured_events.py \
  tests/test_freshness.py tests/test_leak_check.py \
  tests/test_redesign.py tests/test_verified.py tests/test_view_words.py
```

### Release build verification

From the repository root, use `site/.venv` for the full build and a separate
temporary output directory for each build. The FontTools-only environment
exercises a missing WOFF2 encoder backend. [Site build setup](site/README.md#build--run)

```bash
verify_dir=$(mktemp -d)
site/.venv/bin/python site/build_site.py --release --out "$verify_dir/full"
python3 -E -S site/build_site.py --release --out "$verify_dir/stdlib"
python3 -m venv "$verify_dir/fonttools-only"
"$verify_dir/fonttools-only/bin/python" -m pip install 'fonttools>=4.50'
"$verify_dir/fonttools-only/bin/python" -c 'from importlib.util import find_spec; import fontTools; assert find_spec("brotli") is None and find_spec("brotlicffi") is None'
"$verify_dir/fonttools-only/bin/python" site/build_site.py --release --out "$verify_dir/no-brotli"
```

| Build environment | Body font output |
| --- | --- |
| FontTools and Brotli | Pretendard Regular and Bold WOFF2 subsets and license, CSS weights 400 and 700, and a Regular preload |
| Stdlib only | System font stack |
| FontTools without a Brotli backend | System font stack |

Require a successful exit, the `built … pages` summary and `leak check OK`
from each build. Excalifont and Gaegu must resolve in all three builds, including
the Excalifont preload. Each fallback build should warn once and omit Pretendard
files, preloads, and CSS font URLs. Resolve every HTML font preload and CSS font
URL relative to its document or stylesheet and check that it points to a nonempty
same-origin WOFF2 file. Check that each stylesheet and script's `?v=` matches the
first eight hex digits of the MD5 of its emitted bytes and that the full and
fallback CSS hashes differ. Compare the output routes, canonical URLs, sitemap
membership, and `robots.txt` with the [release route inventory](site/README.md#pages).

Serve the full and fallback outputs locally. Open Home, Finder, a venue detail,
and Archive at mobile and desktop widths. Confirm that text is readable, fonts
load locally without failures, and internal links resolve.

## Deploy

The HANSET operations bot deploys through its external release entry point on
the operations box. It runs `python build_site.py --release` from `site/` and
bundles the Pages Functions for Cloudflare Pages project `hanset`. Frontend
changes use the local build, site tests, and loopback preview. Release builds
must pass the leak check. [Repository rules](AGENTS.md)

## Linux crawler

From the repository root, run `python3 -m crawler --help`. The crawler and
Chrome clipper form a local collection and evidence-review workflow with
SQLite state outside the repository. The Linux runbook covers the external
approval registry, SQLite configuration, loopback intake, and `worker --once`.
[Crawler runbook](crawler/README.md#run)

Run the [Linux crawler unit tests](crawler/README.md#linux-crawler-unit-tests)
from the repository root. Use the [mandatory browser integration suite](crawler/README.md#mandatory-browser-integration)
and its Linux prerequisites for browser or clipper changes.

## Chrome clipper

Load `tools/chrome-clipper/` as an unpacked extension in Chrome developer mode.
Set up its origin and connect it to the Linux crawler's loopback intake using
the [clipper runbook](tools/chrome-clipper/README.md).

From the repository root, run the Node.js tests. For browser or clipper
changes, also run the [mandatory Linux browser suite](crawler/README.md#mandatory-browser-integration).

```bash
node --test tools/chrome-clipper/tests/*.test.mjs
```

## hanset_active

From the repository root, run `python3 -m hanset_active --help`. This collector
has its own CLI and operational state. The operations runbook covers the
external `HANSET_ACTIVE_DIR` and normal operation. The collector runbook
covers the `collect` subcommands. [Operations runbook](docs/active-bot/RUNBOOK.md#commands) · [Collector runbook](docs/active-bot/COLLECTOR.md#commands)

After merge, operations can preview the KST cron additions with
`scripts/install_collect_crons.sh --dry-run` and apply them with
`scripts/install_collect_crons.sh` once the publication handoff is confirmed.
Use `--uninstall --dry-run` to preview removal and `--uninstall` to remove
the managed entries. The existing 04:15 collection, active, twin and Linux
crawler entries stay in the crontab. Read the latest appended metrics line
with `tail -n 1 /workspace/hanset-data/metrics/collect-daily.jsonl`.
[Scheduler and metrics runbook](docs/active-bot/RUNBOOK.md#cron)

From the repository root, use a Python 3.11+ test environment. On Linux, use
Bash and `flock` for cron coverage, `pdftotext` from Poppler for PDF coverage,
and Tesseract with English and Korean language data for OCR coverage.

```bash
python3 -m pip install pytest 'requests[socks]' beautifulsoup4 lxml Pillow
python3 -m pytest -q -rs tests/active_bot
```

See also the [twin and explicit evidence tests](crawler/README.md#twin-and-explicit-evidence-tests)
and [data foundation tests](crawler/README.md#data-foundation-tests).

## Excluded from this repo (kept on the operations box)

Deploy pipeline and credentials, Cloudflare IDs, mail and inbox text, venue
contact details, Instagram/YouTube/video production assets, raw captures,
previews, backups, generated `dist/` and `.venv/` directories, and outreach drafts.
