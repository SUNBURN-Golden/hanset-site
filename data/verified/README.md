# Verified feeds (written by the HANSET operations bot)

- `events.json`: `{"version":1,"updated":"YYYY-MM-DD","events":[{"id","title","category":"hyrox|crossfit|run|sauna|yoga|other","start":"YYYY-MM-DD","end":"YYYY-MM-DD|null","city","venue","official_url","source_url","verified_on","price_note"?}]}`
- `creators.json`: `{"version":1,"updated":"YYYY-MM-DD","creators":[{"id","display_name","handle":"@...","platform":"instagram|youtube|other","profile_url","focus":["hyrox"],"city"?,"verified_on"}]}`

Build rules (`site/verified.py`): https URLs only; missing/invalid required field = item dropped with a WARN;
phone/email-like text = item dropped; events that ended before the build date are skipped; sorted by date (creators A–Z).
Missing or empty file = section hidden, no crash. No photos/avatars are loaded; creators show initials.
`examples/*.json` hold fake data marked `"example": true` — always skipped, used only by `build_site.py --verified-dir data/verified/examples` previews.
