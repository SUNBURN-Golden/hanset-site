
## 2026-10-26 14:3x KST: Closed on field + closed-days guide (PR #49)
- Data: `site/content/closed_days.json` holds confirmed closed days only. Each entry has the text, source URL, source type and check date. Venues not in the file show "Check before you go".
- Guide: /guides/closed-days-korea/ (17 venues). Grand Josun Busan is left out on the owner's instruction. Sparex Goodmorning City is left out because its 24h info comes from a third party.
- To add a venue: add a row only from an official page, operator-run profile or venue reply. Never from Naver. `test_closed_on_field_and_guide` checks it.
- Deployed with scripts/deploy.sh, deploy 179e190f.

## 2026-10-26 14:2x KST: Josun Busan closed day + auto-expiry (follow-up to PR #49)
- Added grand-josun-busan: 3rd Tuesday of every month, from the official page, checked 2026-10-18. The guide now has 18 venues.
- `closed_days.json` entries can have an optional `until` (KST date). After that date the entry hides at build time, both on the venue row and in the guide. `--today` is respected. Set now: Termeden until 2026-10-28, Park Hyatt until 2026-11-12, Orakai until 2026-12-14. Rebuild after those dates and they drop off. Test: `test_closed_days_expire`.
