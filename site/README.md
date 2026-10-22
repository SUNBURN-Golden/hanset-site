# HANSET / 한셋 — static site source

(Historical note: the site is now live at https://hanset.soulbound.co.kr. This repo contains source only; deploys are done by the HANSET operations bot. See ../README.md and ../AGENTS.md.) No trackers, no external requests (fonts are self-hosted Pretendard, OFL).

## Build / run
    cd site
    .venv/bin/python build_site.py            # preview build (noindex) — NEVER deploy this one -> dist/ (noindex, robots Disallow)
    python3 -m http.server 8765 --directory dist --bind 127.0.0.1   # http://127.0.0.1:8765/
    .venv/bin/python build_site.py --release  # indexable build: canonical + sitemap for hanset.soulbound.co.kr
    .venv/bin/python build_site.py --ads some_ads.json --out /tmp/x   # test 광고 labels without touching dist/
(.venv = fonttools+brotli only, for font subsetting; stdlib python3 also builds, falling back to system fonts.)
Restart the http.server after a rebuild (dist/ is recreated).

## Source of truth
- `../boxes.csv ../saunas.csv ../events.csv`: price, last_verified, source URLs, city.
- `content/venues.json`, `content/events.json`: Korean copy for the ONLY entries cleared to publish (facts read on an official page, per VERIFICATION_2026-10-16.md). Rows not listed are not published (53 venue rows, 7 event rows).
- The build REFUSES to publish a box unless CSV confidence is verified/partial, a sauna unless official-page-read, an event unless organizer/official-verified. Blank values render as 확인 중.
- `content/ads.json`: `{ "<venue-slug>": {"ad": true, "note": "..."} }` → 광고 badge on card + banner on page + rel="sponsored" + listed first. Empty today.
- `content/pages/*.html`: about / ads / privacy / corrections text.
- Build ends with a leak check (owner credentials, law-firm terms, third-party source names, tracker domains) and fails if any appear.

## Pages
/ (magazine home) · /finder/ (pinned menu; filters city/type/Sunday/price-known/search, URL-deep-linkable) · /finder/<slug>/ (18 venues, 마지막 확인일 + source) · /hyrox-seoul/ · /events/ + /events.ics · /about/ · /ads/ · /privacy/ · /corrections/ · /join/ (Google sign-up; /join/welcome/ is noindex, sitemap lists /join/ only) · 404.

## Sign-up (Google)
- `functions/api/auth/google.js` + `functions/api/auth/callback/google.js` — Cloudflare Pages Functions, plain JS ES modules, no npm deps. `/join/` is a plain HTML form (GET `/api/auth/google`); the whole OAuth flow runs server-side, so the browser never talks to Google from our pages (CSP keeps `connect-src 'none'`, `form-action 'self' https://accounts.google.com` (Chrome applies form-action to the redirect)).
- Pages secrets: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` (optional `GOOGLE_REDIRECT_URI`; defaults to `https://hanset.soulbound.co.kr/api/auth/callback/google`). No secrets live in this repo.
- D1 binding `DB` → database `hanset-members`; table in `schema.sql` (`members`). Repeat logins refresh email/name only — the original newsletter choice is kept.
- Only the operations bot deploys: it bundles the functions with `npx wrangler pages functions build site/functions --outdir <tmp>` alongside `python build_site.py --release`. Per AGENTS.md there are no deploy scripts or CI in this repo, and frontend workers never hold Cloudflare tokens.

## Placeholders to fill before launch
Resolved 2026-10-18: contact is hanset@soulbound.co.kr only, effective date 2026-10-18, no operator info shown (owner decision).
