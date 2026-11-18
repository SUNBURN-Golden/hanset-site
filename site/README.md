# HANSET / 한셋 — static site source

(Historical note: the site is now live at https://hanset.soulbound.co.kr. This repo contains source only; deploys are done by the HANSET operations bot. See ../README.md and ../AGENTS.md.) No trackers, no external requests (fonts are self-hosted Pretendard, OFL).

## Build / run
    cd site
    python3 -m http.server 8765 --directory dist --bind 127.0.0.1   # http://127.0.0.1:8765/
    .venv/bin/python build_site.py --release  # indexable build: canonical + sitemap for hanset.soulbound.co.kr
(.venv = fonttools+brotli only, for font subsetting; stdlib python3 also builds, falling back to system fonts.)
Restart the http.server after a rebuild (dist/ is recreated).

## Source of truth
- `../boxes.csv ../saunas.csv ../events.csv`: price, last_verified, source URLs, city.
- `content/venues.json`, `content/events.json`: Korean copy for the ONLY entries cleared to publish (facts read on an official page, per VERIFICATION_2026-10-16.md). Rows not listed are not published (53 venue rows, 7 event rows).
- The build REFUSES to publish a box unless CSV confidence is verified/partial, a sauna unless official-page-read, an event unless organizer/official-verified. Blank values render as 확인 중.
- `content/ads.json` must remain `{}`. Advertising output is prohibited; `/ads/` stays 404 and is excluded from the sitemap.
- `content/pages/*.html`: about / privacy / corrections text.
- The build validates all emitted text with the leak checker described below.

### Sunday evidence for Finder

Operations may supply `sunday_evidence` on a published record in
`content/venues.json`. The object requires all four fields.

| Field | Accepted value |
| --- | --- |
| `status` | `yes` for regularly open on Sunday, or `no` for regularly closed |
| `source_kind` | `official_page` |
| `source_url` | Absolute public HTTP(S) URL without embedded credentials |
| `checked_on` | Real `YYYY-MM-DD` date on or before the build's Seoul date |

Operations confirms that the official page explicitly supports the regular
Sunday schedule for the listed location and service. Finder uses the Sunday
source and date independently of the venue's overall verification. Evidence
over 90 days old keeps its filter status and displays 오래됨 (Outdated).

Operations-supplied `sunday_evidence` supersedes the closure-record shim once
present, including when the supplied evidence fails validation. When the field
is absent, the shim accepts only a `closed_days.json` record with the exact
text `Sundays and public holidays`, source label `Official page`, a qualifying
public URL and check date, and no `until`.

Legacy flags, class listings, and narrative hours carry no filter eligibility.
Recognized legacy Sunday statements retain their wording and any explicit
reply provenance. Missing, invalid, or conflicting evidence appears as pending
in Finder. Migrate Sunday information embedded in other schedule wording to
the structured field once operations can confirm it.

## Pages
/ (magazine home) · /finder/ (pinned menu; filters city/type/Sunday/price-known/search, URL-deep-linkable) · /finder/<slug>/ (18 venues, 마지막 확인일 + source) · /hyrox-seoul/ · /events/ + /events.ics · /about/ · /privacy/ · /corrections/ · /join/ (Google sign-up; /join/welcome/ is noindex, sitemap lists /join/ only) · 404.

## Sign-up (Google)
- `functions/api/auth/google.js` + `functions/api/auth/callback/google.js` — Cloudflare Pages Functions, plain JS ES modules, no npm deps. `/join/` is a plain HTML form (GET `/api/auth/google`); the whole OAuth flow runs server-side, so the browser never talks to Google from our pages (CSP keeps `connect-src 'none'`, `form-action 'self' https://accounts.google.com` (Chrome applies form-action to the redirect)).
- Pages secrets: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` (optional `GOOGLE_REDIRECT_URI`; defaults to `https://hanset.soulbound.co.kr/api/auth/callback/google`). No secrets live in this repo.
- D1 binding `DB` → database `hanset-members`; table in `schema.sql` (`members`). Repeat logins refresh email/name only — the original newsletter choice is kept.
- Only the HANSET operations bot deploys, through its external release entry point (release build + Pages Functions bundling run on the operations box; see ../README.md#deploy). Per AGENTS.md there are no deploy scripts or CI in this repo, and frontend workers never hold Cloudflare tokens.

## Placeholders to fill before launch
Resolved 2026-10-18: contact is hanset@soulbound.co.kr only, effective date 2026-10-18, no operator info shown (owner decision).

## Emitted-output leak validation

After rendering, asset copying and font generation, `build_site.py` recursively
checks `.html`, `.svg`, `.css`, `.js`, `.json`, `.xml` and `.ics` files (suffixes
are case-insensitive). Unreadable UTF-8 text fails with `validation.read`;
invalid JSON or unsafe internal entity expansion fails with `validation.parse`.
Validation never fetches URLs or resolves remote XML entities.

Checks include raw and decoded content: HTML/XML entities, bounded internal XML
string entities, JSON keys/string values, JavaScript string/identifier escapes,
CSS escapes and unfolded calendar lines. Comments, metadata, attributes, alt
text, inline CSS/scripts and embedded JSON are covered. Phone and legacy numeric
checks apply to prose/string contexts, excluding SVG geometry and numeric code
or style tokens. Dates, timestamps, hours, prices, hashes and local drawing
coordinates remain valid. The only permitted email is the complete address
`hanset@soulbound.co.kr` (case-insensitive). Korean mobile, regional, internet,
service and explicitly prefixed international phone forms, plus `tel:`/`sms:`
destinations, are rejected.

Stable rules include `contact.phone`, `contact.email`, `wording.advertising`,
`wording.health`, `wording.forbidden`, `resource.tracker`, `resource.external`
and `resource.unresolved`. The wording vocabulary explicitly includes Korean
advertising/sponsorship/partnership/media-kit terms and spacing/punctuation
variants; Korean detox, immunity, weight-loss and cure claims; English detox,
immunity enhancement, weight loss, healing and cure terms. Existing forbidden
terms and tracker markers remain checked. Neutral safety copy is allowed.
Keyword matching cannot identify every paraphrase, and ambiguous numbers cannot
always be distinguished from contact details without context.

External loads are classified by use. Official source anchors, JSON source
strings, calendar `URL`, canonical links, sitemap locations and XML namespaces
are allowed. Automatic loads through scripts/styles, images/srcset, fonts,
media/posters, frames, objects, SVG references, resource-hint links, base URLs,
refresh redirects, hyperlink ping, CSS `url()`/`@import`, remote XML
entities/stylesheets and calendar URI attachments are rejected. Relative URLs
are resolved against the production document URL; parsed origins must match
`SITE_URL` exactly, including scheme and effective port. Protocol-relative URLs,
backslashes and escaped destinations are checked. Local paths, fragments,
self-hosted fonts and embedded `data:` resources pass.

The JavaScript scanner tokenizes strings/comments and inspects `fetch`, XHR
`open`, `sendBeacon`, WebSocket/EventSource, Request, imports/importScripts,
workers/service-worker registration, resource-property writes/setAttribute,
location writes/assign/replace, window.open, CSS insertion and HTML insertion.
It supports string literals, static templates, simple concatenation and simple
immutable constant bindings. A recognized load with an unresolved destination
fails closed. Resource-property writes are conservative when the receiver's DOM
type is unknown. Plain source-link strings, comments and regular expressions do
not become loads merely because they contain URLs or operation names.
This is a static guard: arbitrary computed/obfuscated JavaScript, aliases,
dynamic property names and full control-flow/module analysis are outside its
coverage. The existing restrictive CSP remains in place.

Findings are deduplicated and sorted by relative path and rule. Diagnostics
contain only that path and identifier, never matched values, snippets, URLs,
regexes or parser exceptions:

```text
LEAK-CHECK FAIL: assets/js/example.js [resource.external]
```

A failed check exits with status `2` immediately, before other validators can
print content. It never prints `leak check OK` or the successful build summary.
No production bypass is provided. The collector and clipper are independent and
are not imported or exercised by these tests.

Verification from the repository root:

```bash
python3 -m unittest discover -s tests -p 'test_leak_check.py'
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m pytest -q tests/test_redesign.py tests/test_detail_evidence.py tests/test_drawing_disclaimer.py tests/test_drawing_hangul.py
python3 site/build_site.py --release --out "$(mktemp -d)"
```

Fixtures use generated synthetic contacts in temporary directories. Release
failure tests invoke the real CLI in an isolated repository copy and use its
supported stdlib/font fallback (`python -S`); leak validation is never mocked.
The clean release test checks canonical/robots/sitemap, 404, Search Console
verification, cache-busting hashes, CSP and the absence of an advertising page.
