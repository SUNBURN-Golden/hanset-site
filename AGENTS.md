# AGENTS.md — HANSET(한셋) site lock rules

Read this before touching anything. These rules come from the owner (준태님) and are NOT negotiable inside a PR.
Violating any of them = reject the PR. If a rule seems to conflict with a task, stop and ask the owner in the PR.

## Roles
- **Frontend work**: CrazyFrontendManBOT, **PR-only**. Never push to `main` directly.
- **Merging**: the HANSET 운영 bot reviews and merges hanset-site PRs, then deploys with `build_site.py --release` (authorized by 준태님 on 2026-10-19). Other agents open PRs but do not merge.
- **Deploy**: only the HANSET operations bot (Cloudflare Pages project `hanset`). Frontend workers have **no Cloudflare tokens** and must not ask for, create, or commit any token/secret/key. Do not add deploy scripts or CI deploy steps to the repo.
- Content/data facts (prices, hours, venues, events) are maintained by the HANSET operations bot through the CSVs → `site/content/*.json`. Frontend PRs must not change facts.

## Hard content rules
1. **Public contact is `hanset@soulbound.co.kr` only.** No phone numbers anywhere (HTML, JSON, CSV, comments, alt text). No other email addresses.
2. **Operator/business info is not displayed** anywhere on the site (no operator name, business registration, address, representative).
3. **No ads / sponsorship / partnership / media-kit wording or solicitation**, no "광고·협찬 문의" copy. The `/ads/` route must stay **404** (no page, no link, no sitemap entry). `site/content/ads.json` stays `{}`.
4. **No photos of venues.** Only hand-drawn illustrations (`illustrations/`, `site/static/img/`). No stock/AI "photos", no scraped images.
5. Terminology: use **"부티크 사우나 · 호텔 사우나"**. Never "MZ 사우나". Brand: 영문 `HANSET`, 국문 `한셋` (never "한세트").
6. The site address appears **only in the Instagram profile link field** — not in Instagram bio/captions, and not as a link CTA in channel copy.
7. **Only official-source-confirmed facts are published**, each with 확인일(date) + source; unknown = "확인 중". Third-party / blog / AI-summary prices and hours are never published. Facts older than 90 days show "오래됨".
8. No competition career or personal history of the owner anywhere. No effect claims ("디톡스", "면역력 증가", "살 빠진다" etc.). Keep the safety line on sauna content.
9. No trackers, no external requests/CDNs/fonts (fonts are self-hosted), no analytics scripts. The builder's leak check must keep passing.
10. No selling services/classes/products; no per-lead fees; no affiliate links unless the owner explicitly says so.

## Build / SEO locks
- The site is **public**. Always build with **`python build_site.py --release`** (canonical + sitemap, **no noindex**, robots Allow). A preview build (no flag) re-adds `noindex` and would hide the site from search — never deploy it.
- Keep `site/static/root/google1cc2d472584fa095.html` (Google Search Console verification). Do not delete or rename.
- `/ads/` → 404. Sitemap must list only real pages. `404.html` stays.
- CSS/JS links use content-hash `?v=` cache busting; keep it.

## Layout / UX locks
- Mobile-first. The home page's hand-drawn illustration section stays **at the very bottom** (right above the footer). On mobile it is a compact thumbnail list; `/guide/` shows full-width tap-to-enlarge images. Do not reintroduce large swipe cards or clipped text on mobile.
- Top priority screens: Home `/`, Finder `/finder/` (filters city/type/Sunday/price-known/search, URL-deep-linkable), venue detail `/finder/<slug>/`, `/archive/`.
- Illustrations are SVG with outlined text (identical rendering without fonts), each marked "실제 업장과 다를 수 있어요" with source. Archive illustrations are pending venue replies — do not claim venue endorsement.

## Process
- One topic per PR; describe screens affected with mobile + desktop screenshots; run `python build_site.py --release` and attach the output line (page counts + "leak check OK").
- Do not commit: `dist/`, `.venv/`, backups (`*.bak*`, `_prev*`), logs, mail text, venue contact details, tokens/keys/JWTs.
- Before opening a PR, scan the diff for phone numbers, emails other than hanset@soulbound.co.kr, and secret-like strings.
- Content data changes (CSV/JSON) come from the operations bot; if a frontend change needs new data fields, open an issue and describe the field.
