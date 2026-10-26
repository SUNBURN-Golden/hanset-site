# HANSET redesign implementation record

Owner approval: 2026-10-26. Implement design.md §5 as stacked local branches; no publishing or deployment. Korean copy and source data stay unchanged. Each stage runs the release build and all unit tests before commit. Browser screenshots are review artifacts outside the repository in `/workspace/rd-wt/logs/hanset-prN/` (mobile 390×844 and desktop 1440×900, light and dark).

## PR1 — self-hosted fonts

All screens: Archivo semi-condensed headings and tabular numbers, Source Sans 3 body, build-specific Pretendard Regular Korean fallback. Reuse the approved pre-instanced variable WOFF2 sources and their unmodified SIL OFL licences; subset against all generated HTML and JavaScript, including decoded HTML entities. The old optional-fonttools/system-font policy is superseded in both READMEs.

Validation: `cd site && python3 build_site.py --release` — PASS: built 69 pages, 49 venues (10 boxes, 39 saunas), 5 events, 2 verified-feed events, 2 creators; leak check OK. `python3 -m unittest discover -s tests -p 'test_*.py'` — PASS (85 tests). Foreground Playwright/Chrome review — PASS: fonts loaded, no horizontal overflow, external requests or browser errors at both widths in both schemes. Screenshots: `hanset-pr1/home-{390,1440}-{light,dark}.png`.

The environment provides `python3`, not a `python` executable; release commands use that interpreter.
