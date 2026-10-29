# Longblack article structure spec (structure and measurements only; no code, logo, photos, fonts or copy)
Measured 2026-10-20 01:40 KST with Playwright on public longblack.co pages, 1440x900 and 390x844.
| Item | 1440 | 390 |
|---|---|---|
| Article head width | 720px, 4px dark rule on top | 350px |
| Title | 40px/54px 700 | 32px/44px 700 |
| Byline | one small line + 1px rule below | same |
| Body | 18px/34.2px (1.9), 580px column, about 49 Korean chars per line | 18px, 310px |
| Section title | 26px/32px 700 | 22px/28px |
## HANSET
- Article layout only on magazine articles and /finder/<slug>/ venue detail pages.
- **Superseded 2026-10-26, owner approved:** Home and /finder/ keep their finder structure; color tokens only. Home now follows the approved station-wayfinding hero, count board and price rows (redesign PR4). The interactive map stays below the price board. Finder/detail/archive structural redesign is deferred to PR6.
- **Superseded 2026-10-26, owner approved:** Neutral paper background + one accent (#E4572E, text #B83C1A).
- Current HANSET palette: light background #E4EFEC, surface #F1F7F5, text #10241F, muted #3E5A53, border #688C83, accent #0A6A5A; dark background #0E211E, surface #15302B, text #E2EFEB, muted #9DBDB5, border #5A8379, accent #62D2B9. Dark mode follows the user preference. Status badges retain meaning and use tokens.
- Heading and numbers: self-hosted Archivo (width 87, weights 600–800); body: Source Sans 3; Korean: build-specific Pretendard Regular. SIL OFL licences ship with the fonts.
- Two radii: 2px line bars, 4px panels and controls. City line colours are bars only; names remain in the readable text colour. No purple hues (245–320).

### v3 decision, owner approved 2026-10-26

The mint/teal background, accent and self-hosted Archivo heading decision above
continues across the site in v3, including dark mode. Historical paper/terracotta,
system-heading, light-only and Home-colour-only directions remain superseded.
The previous PR6 deferral above refers to the old stack; the new v3 stack separates
Finder, branch hubs, relationships and Home into PR2–PR5. Article measurements
remain historical reference; branch hub structure is superseded by design-v3.md §3.
Implementation is gated by the operations evidence contract; see
[the v3 record](redesign-v3.md). No factual or Korean copy changes are authorized.
