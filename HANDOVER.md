
## 2026-10-26 14:3x KST: Closed on field + closed-days guide (PR #49)
- Data: `site/content/closed_days.json` holds confirmed closed days only. Each entry has the text, source URL, source type and check date. Venues not in the file show "Check before you go".
- Guide: /guides/closed-days-korea/ (17 venues). Grand Josun Busan is left out on the owner's instruction. Sparex Goodmorning City is left out because its 24h info comes from a third party.
- To add a venue: add a row only from an official page, operator-run profile or venue reply. Never from Naver. `test_closed_on_field_and_guide` checks it.
- Deployed with scripts/deploy.sh, deploy 179e190f.
