# HANSET redesign implementation record

Owner approval: 2026-10-26. Implement design.md §5 as stacked local branches; no publishing or deployment. Korean copy and source data stay unchanged. Each stage runs the release build and all unit tests before commit. Browser screenshots are review artifacts outside the repository in `/workspace/rd-wt/logs/hanset-prN/` (mobile 390×844 and desktop 1440×900, light and dark).

## PR1 — self-hosted fonts

All screens: Archivo semi-condensed headings and tabular numbers, Source Sans 3 body, build-specific Pretendard Regular Korean fallback. Reuse the approved pre-instanced variable WOFF2 sources and their unmodified SIL OFL licences; subset against all generated HTML and JavaScript, including decoded HTML entities. The old optional-fonttools/system-font policy is superseded in both READMEs.

PR1 review fixes: Korean headings intentionally use synthetic bold from the single Regular subset, as permitted by the design. Removed the unconditional Pretendard preload; the browser now loads that subset only when fallback glyphs are needed. Archivo and Source Sans 3 retain their preloads. FIX validation: release build (69 pages, leak check OK), 85 unit tests and mobile/desktop light/dark browser checks passed; screenshots: `hanset-fix-pr1/{home,finder,guide,archive}-{390,1440}-{light,dark}.png`.

Validation: `cd site && python3 build_site.py --release` — PASS: built 69 pages, 49 venues (10 boxes, 39 saunas), 5 events, 2 verified-feed events, 2 creators; leak check OK. `python3 -m unittest discover -s tests -p 'test_*.py'` — PASS (85 tests). Foreground Playwright/Chrome review — PASS: fonts loaded, no horizontal overflow, external requests or browser errors at both widths in both schemes. Screenshots: `hanset-pr1/home-{390,1440}-{light,dark}.png`.

The environment provides `python3`, not a `python` executable; release commands use that interpreter.

## PR2 — tokens and dark mode

PR2 review fixes: illustration image backgrounds use the defined `--surface` token in both themes. Badges have a visible border so they retain a chip boundary on cards with the same surface fill. The creator initials `.vinit` intentionally change from circles to 4px rounded squares under the approved two-radius design. FIX validation: release build (69 pages, leak check OK), 85 unit tests, and mobile/desktop light/dark browser checks passed, including computed illustration backgrounds and badge borders. Guide/archive dark screenshots were visually inspected. Screenshots: `hanset-fix-pr2/{home,finder,guide,archive}-{390,1440}-{light,dark}.png` and `{guide,archive}-image-{390,1440}-{light,dark}.png`.

All screens: approved mint/teal palette, four city bar tokens, 2px/4px radii, visible focus, matching light/dark browser theme colours and favicon. Existing map glyphs inherit theme tokens. The old paper/terracotta and home-colour-only decisions remain explicitly marked superseded in `docs/longblack-structure-spec.md`, including its former line 13. No HANSET `DESIGN.md` or `DECISIONS` file exists in this worktree.

Validation: release build — PASS, 69 pages / 49 venues / 5 events / 2 verified-feed events / 2 creators; leak check OK. Full unit suite — PASS (85 tests). Palette hue and text/UI contrast checks — PASS (AA text, 3:1 borders). Foreground browser review — PASS at mobile/desktop, light/dark, no overflow/errors/external requests. Screenshots: `hanset-pr2/home-{390,1440}-{light,dark}.png` and full-page counterparts. Diff phone/email/secret scan — PASS.

## PR3 — labels

PR3 review fixes: removed redundant trailing semicolons from the filter label, fact heading and creator initial rules, and normalized the mobile lead rule indentation. FIX validation: release build (69 pages, leak check OK), 85 unit tests and mobile/desktop light/dark browser checks passed; screenshots: `hanset-fix-pr3/{home,finder,guide,archive}-{390,1440}-{light,dark}.png`.

Home: remove the English hero kicker. Shared CSS: remove positive letter spacing and forced uppercase from brand/eyebrow/filter/fact/verified-feed labels. Korean strings stay unchanged.

Validation: release build — PASS, 69 pages / 49 venues / 5 events / 2 verified-feed events / 2 creators; leak check OK. Full unit suite — PASS (85 tests). Foreground browser review — PASS at mobile/desktop in light/dark; loaded fonts, no overflow/errors/external requests. Screenshots: `hanset-pr3/home-{390,1440}-{light,dark}.png`. An extra full-page capture exceeded Chrome’s timeout on the long explorer page; the required viewport captures were rerun successfully. Diff phone/email/secret scan — PASS.

## PR4 — home wayfinding layout

PR4 review fixes: restored sticky header positioning, hidden navigation scrollbars and a visible active Finder underline. Illustration anchor offsets are now 120px to clear the taller mobile header. The 1240px desktop width applies only to the home main and shared header; Finder/detail/archive keep their 1080px wrap. The mobile hero assertion starts at the actual 900px breakpoint. Header strip and number-board totals both sum `city_counts`, including when the venue list contains other city keys. The proposed relocation of the removed second English hero sentence to the Finder intro is recorded in the external copy todo for follow-up; no Korean text changes. FIX validation: release build (69 pages, leak check OK), 86 unit tests and mobile/desktop screenshots in both themes passed (`hanset-fix-pr4/{home,finder,guide,archive}-{390,1440}-{light,dark}.png`). Browser checks at 320/360/768/900/901/1024/1920px passed for overflow, sticky-header archive anchor clearance, Finder deep links and map selection.

Home and shared header: flat hero, count board, city links and proportional count strip, footer-only Instagram, and three price rows confirmed on official pages, preserving the explorer’s existing default order among eligible entries. Prices supported only by venue replies (for example Sisu House) are excluded from this new board; their existing explorer records remain unchanged. The map and full explorer follow the prices. City/type totals are computed from published venues. Price labels, Korean venue names, caveats, dates, official source links and stale labels are retained; these make price rows taller than the mockup. The hand-drawn section stays last above the footer, with compact mobile thumbnails. The two old home markup/CSS assertions are updated in this PR, plus a regression for order/counts/price provenance.

Validation: release build — PASS, 69 pages / 49 venues / 5 events / 2 verified-feed events / 2 creators; leak check OK. Full unit suite — PASS (86 tests). Foreground browser review — PASS: home at 320/360/390/768/1024/1440/1920 in light/dark, no horizontal overflow, new controls at least 44×44px; city/type deep links, map selection and reduced motion work. Generated internal links/assets and full-page Hangul font coverage — PASS. The initial browser expectation for Jeju+sauna was corrected to the existing three saunas (Jeju also has one box), then the check passed. Mobile/desktop Finder/detail/archive smoke renders — PASS. Diff phone/email/secret scan — PASS. No CSV, content JSON, verified.py or illustration source changes.

Screenshots: `hanset-pr4/home-{390,1440}-{light,dark}.png`, `prices-390.png`, `illustrations-390.png`, and `{finder,detail,archive}-{390,1440}.png`. Proposed Korean-name removal and optional check-label translation are deferred in `/workspace/rd-wt/logs/hanset-copy-todo.md`.

Accepted exceptions: the required hand-drawn art stays at the bottom of home and on guide/archive pages; the mandated Korean term keeps its middle dot. PR6 Finder/detail/archive structural redesign is follow-up work, outside this stack.

## PR5 — metadata separators and arrow suffixes

Venue cards/previews and fact-source lines, home race strip, home magazine metadata and the footer use separate cells. Specified English link suffixes lose their arrows. The English large-spa label uses a comma in both badges and the matching filter. Provenance tests now check the preserved reply/date words rather than decorative punctuation; official/reply source distinctions remain covered. Source links, shortened section links and footer links retain 44px targets.

Validation: release build — PASS:

```text
built 69 pages, 49 venues (10 boxes, 39 saunas), 5 events, 2 verified-feed events, 2 creators -> /workspace/rd-wt/hanset/site/dist  (13839 KB total)
leak check OK (no owner credentials / third-party-source names / trackers)
```

Full unit suite — PASS (87 tests). Foreground browser review — PASS at mobile/desktop, light/dark; responsive 320–1920 checks, new 44px targets, filter deep links, map preview, reduced motion, generated links/assets, loaded fonts and Hangul coverage — PASS. Representative Finder/detail/archive renders — PASS. Screenshots: `hanset-pr5/home-{390,1440}-{light,dark}.png`, `prices-390.png`, `illustrations-390.png`, `{finder,detail,archive}-{390,1440}.png`; the illustration capture waits for lazy image decoding. Diff phone/email/secret scan — PASS.

Korean text-token inventory is unchanged from the base builder. CSVs, content JSON, verified.py and illustrations/handdraw.py are unchanged across the whole stack. Middle dots inside existing fact values, names and the mandated Korean terminology remain verbatim. Remaining Finder/detail/archive structural and copy cleanup belongs to the out-of-scope PR6 follow-up.

## Release output and final audit

Every stage emitted the same page-count prefix and leak-check line below (PR1/PR2/PR3/PR4/PR5 artifact totals: 13807/13814/13814/13812/13839 KB):

```text
built 69 pages, 49 venues (10 boxes, 39 saunas), 5 events, 2 verified-feed events, 2 creators
leak check OK (no owner credentials / third-party-source names / trackers)
```

After the official-price eligibility correction, PR4 passed its 86-test suite, release build and mobile/desktop browser checks again. PR5 was rebased onto that correction, then passed the 87-test suite, release build and browser checks again. Final checks passed for stacked ancestry, the complete diff phone/email/secret scan, unchanged Korean text inventory and source data, release indexing, real-page sitemap, /ads/ absence, 404 and Search Console verification. No push, remote PR creation, merge or deployment was performed.

Whitespace audit: source changes pass `git diff --check origin/main` when the two supplied OFL text files are excluded. The unmodified Archivo licence contains one trailing space, and Source Sans 3 uses upstream CRLF line endings; those licence-only warnings are retained to preserve the supplied files byte for byte.
