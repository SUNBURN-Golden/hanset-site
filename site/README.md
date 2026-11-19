# HANSET / 한셋 — static site source

The Python builder reads checked-in publication inputs and emits the static
site with self-hosted web fonts. The HANSET operations bot owns publication.
[Release procedure](../README.md#deploy) · [Repository rules](../AGENTS.md)

## Build / run

From the repository root, set up the build environment and generate the
release into `site/dist/`.

```bash
python3 -m venv site/.venv
site/.venv/bin/python -m pip install -r site/requirements.txt
site/.venv/bin/python site/build_site.py --release
```

The site build environment uses FontTools and Brotli to subset Pretendard.
If FontTools or its WOFF2 Brotli backend is unavailable, body text uses the
system font stack. The bundled Excalifont and Gaegu display fonts stay available.
Use `--release` for public output. A preview build without the flag emits
`noindex,nofollow` and disallows crawling in `robots.txt`.

From the repository root, serve the generated static files on loopback.

```bash
python3 -m http.server 8765 --directory site/dist --bind 127.0.0.1
```

Open `http://127.0.0.1:8765/`. Google sign-up requires the separate
[Pages Functions](#sign-up-google). Run the [site tests](../README.md#site-tests)
and the [release verification matrix](../README.md#release-build-verification)
from the repository root.

## Source of truth

Operations maintains [boxes.csv](../boxes.csv), [saunas.csv](../saunas.csv),
and [events.csv](../events.csv), along with the published copy in
[`content/`](content/). Published facts carry an official source and check date.
Unchecked values stay pending (`확인 중`), and evidence older than 90 days
shows `오래됨` (Outdated). [Public verification policy](content/pages/faq.html)

For the main venue and event records, the builder requires a matching CSV row,
an entry in the publication JSON, and matching English copy. The CSV confidence
gate accepts `verified` or `partial` for boxes, `high`-prefixed confidence for
official-page-checked saunas, and `verified` or `dates verified` prefixes for
organizer- or official-source-checked events. [Publication gates](build_site.py)

Operations also maintains the optional event and creator feeds. Their validation
rules and preview examples are documented in the [verified feeds runbook](../data/verified/README.md).
[`content/ads.json`](content/ads.json) must remain `{}`. The builder checks
emitted text before reporting a successful release. [Leak validation](#emitted-output-leak-validation)

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

| Release surface | Output |
| --- | --- |
| Home and Finder | `/`, `/finder/`, and `/finder/<slug>/` for each published venue. Home ends with a compact illustration list on mobile. Finder shares its search text, city, type, sauna subtype, Sunday status, confirmed-price and official-list filters, and sort order through URL query parameters. |
| Categories | `/sauna/`, `/crossfit/`, `/hyrox/`, and `/gym/` are always generated. Each lists the published venues that qualify for that category. |
| Illustrations | `/archive/` is the hand-drawn venue gallery. `/guide/` explains Finder and includes tap-to-enlarge illustrations. |
| Visitor guides | `/guides/` links to the visitor guide pages listed below. |
| Magazine | `/magazine/` is always generated. `/magazine/<slug>/` comes from the builder's `ARTICLES` list. Markdown in [`content/articles/_archive/`](content/articles/_archive/) is unpublished. |
| Events and optional pages | `/events/` and `/events.ics` are always generated. `/hyrox-seoul/` requires a published event flagged as a feature and stays available after that event ends. `/creators/` requires at least one valid creator feed entry. Both optional pages appeared in the checked release. |
| Information and sign-up | `/about/`, `/faq/`, `/privacy/`, `/corrections/`, and `/join/` are indexable in release. `/join/welcome/` is generated with `noindex,nofollow` and has no canonical URL or sitemap entry. |
| Release files and exclusions | The output root contains `404.html` and the [tracked Search Console verification file](static/root/google1cc2d472584fa095.html). `/ads/` returns 404 and has no generated page, internal link, or sitemap entry. |

Release `robots.txt` allows crawling and points to `sitemap.xml`. The sitemap
lists the indexable page routes with their production canonical URLs and
omits `404.html`, the verification file, and `events.ics`.

### Visitor guide pages

- `/guides/tattoo-friendly-saunas-korea/`
- `/guides/hotel-saunas-korea-non-guests/`
- `/guides/crossfit-hyrox-drop-in-korea/`
- `/guides/closed-days-korea/`

## Sign-up (Google)

- The `/join/` form sends `GET /api/auth/google`. Cloudflare Pages Functions handle the OAuth redirect and callback. The functions are JS ES modules without npm dependencies. [Start handler](functions/api/auth/google.js) · [Callback handler](functions/api/auth/callback/google.js)
- Pages secrets: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` (optional `GOOGLE_REDIRECT_URI`; defaults to `https://hanset.soulbound.co.kr/api/auth/callback/google`). No secrets live in this repo.
- The D1 binding `DB` uses the `hanset-members` database and its `members` table. Repeat logins refresh email and name and preserve the original newsletter choice. [Schema](schema.sql)

## Public contact

The public contact is `hanset@soulbound.co.kr`.

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

The [root site test targets](../README.md#site-tests) include the leak-check
suite. Fixtures use generated synthetic contacts in temporary directories. Release
failure tests invoke the real CLI in an isolated repository copy and use its
supported stdlib/font fallback (`python -E -S`); leak validation is never mocked.
The clean release test checks canonical/robots/sitemap, 404, Search Console
verification, font references, cache-busting hashes, CSP and the absence of an
advertising page.
