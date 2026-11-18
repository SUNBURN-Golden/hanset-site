#!/usr/bin/env python3
"""HANSET / 한셋 static site builder.

Reads ./boxes.csv ./saunas.csv ./events.csv (single source of truth for numbers, dates, URLs) and
content/venues.json + content/events.json (Korean copy for entries cleared to publish) and writes ./dist.
Only stdlib required; fonttools+brotli (in .venv) are used to subset Pretendard into small woff2 files.
Nothing is deployed, nothing is sent. No trackers, no external requests.

  .venv/bin/python build_site.py                 # preview build (noindex)
  .venv/bin/python build_site.py --release       # indexable + canonical/sitemap for hanset.soulbound.co.kr
"""
import argparse, csv, datetime as dt, html, json, re, shutil, sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse
from zoneinfo import ZoneInfo
import verified as VF

ROOT = Path(__file__).resolve().parent
DATA = ROOT.parent
DIST = ROOT / "dist"
VENUE_SKETCH = ROOT / "static/venue-sketch"
VERIFIED = DATA / "data/verified"
SITE_URL = "https://hanset.soulbound.co.kr"
BRAND_KO, BRAND_EN = "한셋", "HANSET"
FINDER_LABEL = "Drop-in & Sauna Finder"
CITY_KO = {"Seoul": "서울", "Busan": "부산", "Jeju": "제주", "Gyeonggi": "경기"}
CITY_EN = {"Seoul": "Seoul", "Busan": "Busan", "Jeju": "Jeju", "Gyeonggi": "Gyeonggi"}
MON = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
KIND_EN = {"호텔 사우나": "Hotel Sauna", "부티크 사우나": "Boutique Sauna", "대형 스파·온천": "Large Spa · Hot Spring", "크로스핏 박스": "CrossFit Box",
           "HYROX·스트렝스 짐": "HYROX · Strength Gym", "호텔·클럽 사우나": "Hotel · Club Sauna"}
PENDING = "Not confirmed yet"
CITY_KEY = {"Seoul": "seoul", "Busan": "busan", "Jeju": "jeju", "Gyeonggi": "gyeonggi"}
WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SAFETY = "Heart disease, pregnancy, after drinking, or feeling unwell? Skip the sauna and cold plunge, or ask a doctor first. Not medical advice."
STALE_DAYS = 90
OUTDATED = "오래됨 (Outdated)"
FORBIDDEN = [r"\b442\b", r"KWANG", r"크왕", r"우승", r"로스쿨", r"법무", r"법학", r"변호사", r"\bMZ\b", r"saunaday", r"사우나데이",
             r"moduisauna", r"crossfitlee", r"LUWEI", r"한세트", r"땀살롱", r"PRIVATE-DM",
             # no ad/sponsor solicitation, no excluded venue, no AI-editor wording, no health overclaims (EN + KO)
             r"(?i)sponsor", r"(?i)advertis", r"(?i)partnership", r"(?i)media[ -]?kit", r"(?i)collaborat", r"(?i)\bAI editor",
             r"(?i)runport", r"런포트", r"버핏", r"스폰서", r"협찬", r"제휴", r"미디어킷", r"노출료", r"광고문의", r"가상의 AI",
             r"(?i)\bdetox", r"(?i)\bheal(s|ing|ed)?\b", r"(?i)\bcures?\b", r"(?i)fat[- ]burn", r"(?i)\bMZ sauna"]

E = html.escape
def read_csv(name):
    with open(DATA / name, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))
def won(s): return "₩{:,}".format(int(s)) if s and s.strip().isdigit() else None
def host(u): return urlparse(u).netloc.replace("www.", "")
def ymd(s): return dt.date.fromisoformat(s)
def dot(d): return d.strftime("%Y.%m.%d")
def is_outdated(checked_on, today):
    return (today - checked_on).days > STALE_DAYS

def freshness(checked_on, today):
    return badge(OUTDATED, "badge-old") if checked_on and is_outdated(checked_on, today) else ""

def closure_provenance(c, today):
    raw = c.get("checked") or ""
    checked = parse_evidence_date(raw)
    checked_text = dot(checked) if checked else f'{raw} (date not confirmed)'.strip()
    return (f'Checked {E(checked_text)} · <a href="{E(c["url"])}" rel="noopener noreferrer">'
            f'{E(c["source"])} · {E(host(c["url"]))}</a> {freshness(checked, today)}')
def wd(d): return WD[d.weekday()]
def span(a, b, ref_year):
    def f(d):
        return (f"{d.month}/{d.day} ({wd(d)})" if d.year == ref_year else f"{d.year}.{d.month}.{d.day} ({wd(d)})")
    return f(a) if a == b else f"{f(a)}–{f(b)}"

FOREIGNER = json.loads((ROOT / "content/foreigner.json").read_text(encoding="utf-8"))
CLOSED_ALL = {k: c for k, c in json.loads((ROOT / "content/closed_days.json").read_text(encoding="utf-8")).items() if not k.startswith("_")}
def active_closed(today):
    return {k: c for k, c in CLOSED_ALL.items() if not c.get("until") or ymd(c["until"]) >= today}
CLOSED_CHECK = "Check before you go"
def closed_row(v, today):
    c = active_closed(today).get(v["slug"])
    if not c: return f'<tr><th>Closed on</th><td class="unk">{CLOSED_CHECK}</td></tr>'
    return (f'<tr><th>Closed on</th><td>{E(c["closed"])}<br>'
            f'<small class="fact-source">{closure_provenance(c, today)}</small></td></tr>')

try: CLOSED_MONTH = json.loads((ROOT / "content/closed_month.json").read_text(encoding="utf-8"))
except FileNotFoundError: CLOSED_MONTH = {}  # month summary is optional; absent file = no block
def closed_month_block(today, by_slug):
    """English 'this month's closed days' summary for the closed-days guide, from content/closed_month.json.
    Hides after the month ends; items whose date is already past drop at build time (main()'s today, KST)."""
    if not CLOSED_MONTH.get("items"): return ""
    y, m = (int(n) for n in CLOSED_MONTH["month"].split("-"))
    if today > dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1): return ""
    rows = []
    for it in CLOSED_MONTH["items"]:
        d = ymd(it["date"])
        if d < today: continue
        links = []
        for slug in it["slugs"]:
            v, c = by_slug.get(slug), CLOSED_ALL.get(slug)
            if not v or not c: continue
            note = (" (may change around holidays)" if "may change" in c["closed"] else "") + (" (maintenance)" if it.get("note") == "maintenance" else "")
            links.append(f'<span class="closure-entry"><a href="{E(c["url"])}" rel="noopener noreferrer">{E(v["title"])}</a>{note}'
                         f'<small class="fact-source">{closure_provenance(c, today)}</small></span>')
        if links: rows.append(f'<li>{MON[d.month]} {d.day} ({wd(d)}): {" · ".join(links)}</li>')
    if not rows: return ""
    return (f'<section class="closed-month"><h2>{E(CLOSED_MONTH["title"])}</h2><ul class="clean">{"".join(rows)}</ul>'
            '<p class="muted">Hours can change, so check before you go.</p></section>')
FCHK_KEYS = [("tattoo", "Tattoos"), ("card", "Foreign card payment"), ("english", "English booking / staff"), ("dropin", "Drop-in for visitors")]
SAUNA_PKG_KEYS = [
    ("public_access", "General public access"),
    ("restriction", "Guest / member restriction"),
    ("booking", "Booking method"),
    ("kr_phone", "Korean phone number"),
    ("card", "Foreign card payment"),
    ("english", "English support / guide"),
    ("full_price", "Full admission rates"),
    ("scrub_fee", "Body scrub (seshin) fee"),
    ("last_entry", "Last entry cutoff"),
    ("closures", "Regular & temp closures"),
    ("age_tattoo_attire", "Age · tattoo · attire rules"),
]
def _is_confirmed(val):
    return bool(val and val.strip() and val.strip().lower() not in ("not confirmed", "not stated for non-guests.", "확인 중", "확인 중 (not confirmed)"))
def fchk(v, today):
    d = FOREIGNER.get(v["slug"], {})
    keys = SAUNA_PKG_KEYS if (v["kind_key"] == "sauna" and "public_access" in d) else FCHK_KEYS
    rows = "".join(
        f'<tr><th>{lab}</th>' + (f'<td>{E(d[k])}{fact_provenance(v, d[k], today)}</td>' if _is_confirmed(d.get(k, "")) else '<td class="unk">Not confirmed</td>') + '</tr>'
        for k, lab in keys
    )
    return f'<h2>Foreigner &amp; visitor access checklist</h2><table class="facts"><tbody>{rows}</tbody></table>'

GUIDES = [  # (slug, title, description); page bodies are built in guides()
    ("tattoo-friendly-saunas-korea", "Tattoos and saunas in Korea", "What official pages of Korean hotel saunas and spas say about tattoos. Verified, dated, sourced."),
    ("hotel-saunas-korea-non-guests", "Hotel saunas in Korea: can non-guests get in?", "Which Korean hotel saunas are open to visitors without a room, per their official pages."),
    ("crossfit-hyrox-drop-in-korea", "CrossFit & HYROX drop-in in Korea", "CrossFit and HYROX boxes in Korea that list drop-ins or English support on their official pages."),
    ("closed-days-korea", "Closed days: saunas and gyms in Korea", "Confirmed closed days for Korean saunas, spas and CrossFit boxes. Each with its source and check date.")]

def guides(site, venues, today):
    sau = [v for v in venues if v["kind_key"] == "sauna"]; box = [v for v in venues if v["kind_key"] == "box"]
    def tbl(vs, key, head):
        confirmed = [v for v in vs if _is_confirmed(FOREIGNER.get(v["slug"], {}).get(key, ""))]
        excluded_cnt = len(vs) - len(confirmed)
        r = "".join(f'<tr><th><a href="/finder/{v["slug"]}/">{E(v["title"])}</a><br><span class="muted">{E(v["city_ko"])}</span></th><td>{E(FOREIGNER[v["slug"]][key])}</td>'
                    f'<td>{fact_provenance(v, FOREIGNER[v["slug"]][key], today, link_claims=True)}</td></tr>' for v in confirmed)
        excl_note = f'<p class="muted">{excluded_cnt} open venue(s) where "{head}" is still Not confirmed on the official page are excluded from this table. See each venue page in the <a href="/finder/">Finder</a> for full details.</p>' if excluded_cnt else ""
        return f'<table class="facts"><thead><tr><th>Venue</th><th>{head}</th><th>Checked · Source</th></tr></thead><tbody>{r}</tbody></table>{excl_note}'
    note = '<p class="muted">Official pages only. Every line links to its source and check date. Venues where a condition is Not confirmed, pre-opening, or temporarily closed are excluded from recommendations below.</p>'
    bodies = {"tattoo-friendly-saunas-korea": "<h1>Tattoos? Read this first.</h1><p class=\"lead\">Some Korean saunas limit entry with tattoos. Here’s exactly what each official page says. Nothing more.</p>" + note + tbl(sau, "tattoo", "Tattoo rule"),
              "hotel-saunas-korea-non-guests": "<h1>No room? Some doors still open.</h1><p class=\"lead\">Hotel saunas in Seoul, Busan and Jeju. Who gets in, straight from the official pages.</p>" + note + tbl(sau, "dropin", "Open to visitors?") + f'<p class="safety">{SAFETY}</p>',
              "crossfit-hyrox-drop-in-korea": "<h1>Traveling? Keep training.</h1><p class=\"lead\">CrossFit and HYROX boxes in Korea. Drop-in and English support, from official pages only.</p>" + note + tbl(box, "dropin", "Drop-in") + "<h2>English support</h2>" + tbl(box, "english", "English")}
    by_slug = {v["slug"]: v for v in venues}
    crow = "".join(f'<tr><th><a href="/finder/{k}/">{E(by_slug[k]["title"])}</a><br><span class="muted">{E(by_slug[k]["city_ko"])}</span></th><td>{E(c["closed"])}</td>'
                   f'<td>{closure_provenance(c, today)}</td></tr>'
                   for k, c in sorted(((k, c) for k, c in active_closed(today).items() if k in by_slug),
                                      key=lambda kc: (by_slug[kc[0]]["city_key"] != "seoul", by_slug[kc[0]]["city_key"], by_slug[kc[0]]["title"])))
    bodies["closed-days-korea"] = ('<h1>Closed today? Check first.</h1><p class="lead">Some Korean saunas and gyms close one day a month. Others close every week. Here are the ones we’ve confirmed.</p>'
        + closed_month_block(today, by_slug)
        + '<p class="muted">Only places we’ve confirmed are listed, each with its source and check date. Not on the list? Check before you go. Holidays can change hours.</p>'
        f'<table class="facts"><thead><tr><th>Venue</th><th>Closed on</th><th>Checked · Source</th></tr></thead><tbody>{crow}</tbody></table>'
        '<p class="muted">Every other venue in the <a href="/finder/">Finder</a> shows “Check before you go” until its closed day is confirmed.</p>')
    G = [(s, t, d, bodies[s]) for s, t, d in GUIDES]
    links = "".join(f'<li><a href="/guides/{s}/">{E(t)}</a></li>' for s, t, _, _ in G)
    for s, t, d, b in G:
        site.add(f"/guides/{s}/", t, d, f'<div class="page">{b}<h2>More guides</h2><ul class="clean">{links}</ul></div>', "visitors")
    site.add("/guides/", "Guides for visitors to Korea", "Verified guides for international visitors: saunas, tattoos, hotel saunas, CrossFit and HYROX drop-ins in Korea.",
             f'<div class="page"><h1>Visiting Korea? Start here.</h1><p class="lead">Short guides. Official facts only.</p><ul class="clean">{links}</ul></div>', "visitors")

# ------------------------------------------------------------------ data
def load(today):
    ads_error = "ERROR: content/ads.json must contain an empty JSON object."
    try:
        ads = json.loads((ROOT / "content/ads.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        sys.exit(ads_error)
    if ads != {}:
        sys.exit(ads_error)

    content = json.loads((ROOT / "content/venues.json").read_text(encoding="utf-8"))
    en = json.loads((ROOT / "content/venues_en.json").read_text(encoding="utf-8"))
    for path, records in (("content/venues.json", content), ("content/venues_en.json", en)):
        if any("ad" in record for name, record in records.items()
               if not name.startswith("_") and isinstance(record, dict)):
            sys.exit(f"ERROR: {path} must not contain venue ad flags.")

    ev_content = json.loads((ROOT / "content/events.json").read_text(encoding="utf-8"))
    ev_en = json.loads((ROOT / "content/events_en.json").read_text(encoding="utf-8"))
    rows = {"boxes": {r["name"]: r for r in read_csv("boxes.csv")}, "saunas": {r["name"]: r for r in read_csv("saunas.csv")}}
    venues, skipped = [], []
    for name, c in content.items():
        if name.startswith("_"): continue
        r = rows[c["csv"]].get(name)
        if r is None: sys.exit(f"ERROR: {name!r} not found in {c['csv']}.csv")
        conf = r["confidence"].strip().lower()
        if c["csv"] == "boxes":
            if conf not in ("verified", "partial"): sys.exit(f"ERROR: refusing to publish box {name!r} with confidence {conf!r}")
            status = conf
            if c.get("status") and c["status"] != status: sys.exit(f"ERROR: status mismatch for {name}")
            src = r["price_source_url"] or r["website"] or r["cf_listing_url"]
            extra = [("CrossFit official affiliate list", r["cf_listing_url"])] if r["cf_listing_url"] and r["cf_listing_url"] != src else []
            addr = r["address"]
            kind_key = "box"; sunday = c.get("sunday") or {"yes": "yes", "no": "no"}.get(r["sunday_open"], "unk")
            price = won(r["drop_in_price_krw"]); site = r["website"] or ""; book = r["booking_url"] or ""
            chk = r.get("official_check_date", "")
            cf_ok = r.get("cf_official_check") == "yes" and bool(r.get("cf_official_url")); hy_ok = r.get("hyrox_official_check") == "yes" and bool(r.get("hyrox_official_url"))
            off = dict(date=chk, cf=r.get("cf_official_url", "") if cf_ok else "", hyrox=r.get("hyrox_official_url", "") if hy_ok else "")
            if cf_ok: extra = [("CrossFit official affiliate page", off["cf"])]
            if hy_ok: extra = extra + [("HYROX official gym list", off["hyrox"])]
        else:
            if not conf.startswith("high"): sys.exit(f"ERROR: refusing to publish sauna {name!r} with confidence {conf!r}")
            status = c["status"]
            src = r["source_url"]; extra = []; addr = ""
            kind_key = "sauna"; sunday = c.get("sunday") or "unk"; off = dict(date="", cf="", hyrox="")
            price = won(r["price_krw"]); site = r["website"] or ""; book = ""
        ptxt = price or PENDING
        def fill(s): return s.replace("{price}", ptxt)
        x = en.get(c["slug"])
        if x is None: sys.exit(f"ERROR: no English copy for {c['slug']} in venues_en.json")
        if len(x["facts"]) != len(c["facts"]): sys.exit(f"ERROR: English facts mismatch for {c['slug']}")
        facts = [[k, en_fact(k, fill(v), en_only(x["location"]))] for k, v in x["facts"]]
        facts_ko = [[k, v] for k, v in c["facts"]]
        lv = ymd(r["last_verified"])
        v = dict(c=c, name=name, slug=c["slug"], kind_key=kind_key, kind=KIND_EN[c["kind"]], status=status, confidence=conf, title=en_only(x["title"]), title_ko=c["title"],
                 city=r["city"], city_ko=CITY_EN.get(r["city"], r["city"]), city_key=CITY_KEY.get(r["city"], r["city"].lower()),
                 location=en_only(x["location"]), location_ko=c["location"], area=x.get("area", ""), summary=x["summary"], facts=facts, facts_ko=facts_ko, tips=x.get("tips", []), unverified=[fill(u) for u in x.get("unverified", [])],
                 tags=x.get("tags", []), price=price, price_label=x.get("price_label", "Price"),
                 sunday=sunday, last_verified=lv, stale=is_outdated(lv, today), source=src, extra_sources=extra,
                 website=site, booking=book, address=addr, hyrox=c.get("hyrox") is True, pick=bool(c.get("pick")),
                 price_caveat=x.get("price_caveat", ""), sub=("box" if kind_key == "box" else (c.get("sub") or ("hotel" if "호텔" in c["kind"] else "boutique"))),
                 flags=c.get("flags", {}), order=len(venues), off=off)
        venues.append(v)
    for kind in rows:
        for name, r in rows[kind].items():
            if name not in content: skipped.append((kind, name, r["confidence"][:28]))
    events = []
    for name, c in ev_content.items():
        if name.startswith("_"): continue
        r = next((x for x in read_csv("events.csv") if x["name"] == name), None)
        if r is None: sys.exit(f"ERROR: event {name!r} not in events.csv")
        conf = r["confidence"].lower()
        if not (conf.startswith("verified") or conf.startswith("dates verified")): sys.exit(f"ERROR: refusing to publish event {name!r} ({conf})")
        s, e = ymd(r["start_date"]), ymd(r["end_date"])
        x = ev_en.get(c["slug"])
        if x is None or len(x["facts"]) != len(c["facts"]): sys.exit(f"ERROR: English copy missing/mismatched for event {c['slug']}")
        events.append(dict(c=c, name=name, slug=c["slug"], title=x["title"], kind=x["kind"], start=s, end=e, city=x["city"], venue=x["venue"],
                           facts=x["facts"], note=x.get("note", ""), url=c["official_url"], source_label=x["source_label"],
                           last_verified=ymd(r["last_verified"]), feature=bool(c.get("feature"))))
    events.sort(key=lambda x: x["start"])
    ev_skipped = [r["name"] for r in read_csv("events.csv") if r["name"] not in ev_content]
    return venues, events, skipped, ev_skipped

# ------------------------------------------------------------------ verified feeds (data/verified/*.json)
CAT_LABEL = {"hyrox": "HYROX", "crossfit": "CrossFit", "run": "Run", "sauna": "Sauna", "yoga": "Yoga", "other": "Event"}
CAT_SHAPE = {"hyrox": '<rect x="2" y="2" width="10" height="10" rx="2"/>', "crossfit": '<path d="M7 1.5 12.5 12h-11z"/>',
             "run": '<circle cx="7" cy="7" r="5.5"/>', "sauna": '<path d="M1.5 9.5a5.5 5.5 0 0 1 11 0z"/>',
             "yoga": '<path d="M7 1 13 7 7 13 1 7z"/>', "other": '<circle cx="7" cy="7" r="3"/>'}
NEW_TAB = '<span class="vh"> (opens in a new tab)</span>'
def cat_icon(c): return f'<svg class="vshape" viewBox="0 0 14 14" width="14" height="14" aria-hidden="true" focusable="false">{CAT_SHAPE[c]}</svg>'
def vevent_card(e, today):
    s, n = e["start"], e["end"]
    day = str(s.day) if s == n else (f"{s.day}–{n.day}" if (s.year, s.month) == (n.year, n.month) else str(s.day))
    till = f'<span class="vtill">to {MON[n.month]} {n.day}</span>' if (s.year, s.month) != (n.year, n.month) else ""
    yr = f" {s.year}" if s.year != today.year else ""
    price = f'<p class="vnote">{E(e["price_note"])}</p>' if e["price_note"] else ""
    return (f'<article class="vcard" id="v-{e["id"]}"><time class="vdate" datetime="{s.isoformat()}"><span class="vmon">{MON[s.month]}{yr}</span><span class="vday">{day}</span>{till}</time>'
            f'<div class="vbody"><span class="vcat vcat-{e["category"]}">{cat_icon(e["category"])}{CAT_LABEL[e["category"]]}</span><h3>{E(e["title"])}</h3>'
            f'<p class="vloc">{E(e["city"])} · {E(e["venue"])}</p>{price}<p class="vmeta">Checked {dot(e["verified_on"])} · <a href="{E(e["source_url"])}" rel="noopener noreferrer">Source</a> {freshness(e["verified_on"], today)}</p></div>'
            f'<a class="vcta" href="{E(e["official_url"])}" target="_blank" rel="noopener noreferrer">Official page<span class="vh"> for {E(e["title"])}</span>{NEW_TAB} <span aria-hidden="true">↗</span></a></article>')
PLAT = {"instagram": "Instagram", "youtube": "YouTube", "other": "Profile"}
def initials(name):
    w = [x for x in re.split(r"[\s._-]+", name) if x]
    return "".join(x[0] for x in w[:2]).upper() or "?"
def creator_card(c, today):
    c = dict(c, display_name=en_only(c["display_name"]) or c["handle"].lstrip("@"))
    focus = " · ".join(CAT_LABEL.get(f, f.capitalize()) for f in c["focus"])
    city = f' · {E(c["city"])}' if c["city"] else ""
    return (f'<article class="vcard vcreator" id="c-{c["id"]}"><span class="vinit" aria-hidden="true">{E(initials(c["display_name"]))}</span>'
            f'<div class="vbody"><h3>{E(c["display_name"])}</h3><p class="vloc">{E(c["handle"])} · {PLAT[c["platform"]]}</p>'
            f'<p class="vfocus">{E(focus)}{city}</p><p class="vmeta">Checked {dot(c["verified_on"])} {freshness(c["verified_on"], today)}</p></div>'
            f'<a class="vcta" href="{E(c["profile_url"])}" target="_blank" rel="noopener noreferrer">View<span class="vh"> {E(c["display_name"])} on {PLAT[c["platform"]]}</span>{NEW_TAB} <span aria-hidden="true">↗</span></a></article>')
def vevents_section(vevents, today):
    if not vevents: return ""
    return (f'<section class="sec vsec" aria-labelledby="vev-h"><div class="sec-h"><h2 id="vev-h">Coming up</h2></div>'
            f'<div class="vlist">{"".join(vevent_card(e, today) for e in vevents)}</div>'
            f'<p class="muted">Official pages only</p></section>')

# ------------------------------------------------------------------ html pieces
CREATORS_LINK = '<a href="/creators/">Creators</a>'
IG_URL = "https://www.instagram.com/hanset.mag/"
IG_SVG = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true" focusable="false"><rect x="3" y="3" width="18" height="18" rx="5"/><circle cx="12" cy="12" r="4"/><circle cx="17.3" cy="6.7" r="1.1" fill="currentColor" stroke="none"/></svg>'
IG_ATTRS = f'href="{IG_URL}" target="_blank" rel="noopener noreferrer" aria-label="HANSET on Instagram @hanset.mag"'

TABS = (("sauna", "Sauna"), ("crossfit", "CrossFit"), ("hyrox", "HYROX"), ("gym", "Gym"), ("magazine", "Magazine"))
TAB_PATHS = {
    "sauna": '<path d="M5 19h14M7 15c-4-4 4-5 0-10m5 10c-4-4 4-5 0-10m5 10c-4-4 4-5 0-10"/>',
    "crossfit": '<path d="M3 12h18M5 6v12m3-10v8m8-8v8m3-10v12"/>',
    "hyrox": '<path d="m4 8 4-3 5 4-3 4 5 6m-5-6-6 5m9-9 4 3h4"/><circle cx="15" cy="4" r="2"/>',
    "gym": '<path d="m7 7 10 10M3 7l4-4m-2 9 7-7m0 14 7-7m-2 9 4-4"/>',
    "magazine": '<path d="M12 5v15M3 4c4-1 6 0 9 1 3-1 5-2 9-1v15c-4-1-6 0-9 1-3-1-5-2-9-1z"/>',
}


def tab_icon(key):
    return ('<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">{TAB_PATHS[key]}</svg>')


def venue_categories(v):
    """Only published records enter here; never infer HYROX from a name/directory."""
    out = []
    kind = v["c"]["kind"]
    if v["kind_key"] == "sauna": out.append("sauna")
    if kind == "크로스핏 박스": out.append("crossfit")
    if v["c"].get("hyrox") is True: out.append("hyrox")
    if kind == "HYROX·스트렝스 짐" and any(k == "Open gym" and evidence_status(v, val) == "fact" for k, val in v["facts"]):
        out.append("gym")
    return out


def search_text(v):
    return " ".join((v["title"], v["title_ko"], v.get("name") or v["title"], v["location"], v["location_ko"],
                     v["area"], v["city"], CITY_KO.get(v["city"], ""), v["kind"], v["c"]["kind"],
                     " ".join(venue_categories(v)))).lower()


def category_body(key, label, venues, today):
    header = f'<div class="category-head"><h1>{label}</h1></div>'
    safety = f'<p class="safety">{SAFETY}</p>' if key == "sauna" else ""
    if not venues:
        return header + '<p class="count">0 places</p><div class="empty"><p>Coming soon</p></div>' + safety
    return (header + '<form class="filters category-search" role="search"><label class="vh" for="f-q">Search name or area</label>'
            '<input id="f-q" name="q" type="search" placeholder="Search name or area" autocomplete="off"></form>'
            f'<p class="count" id="f-count" role="status" aria-live="polite">{len(venues)} {"place" if len(venues) == 1 else "places"}</p>'
            f'<h2 class="vh">Places</h2><div class="grid" id="finder-list">{"".join(venue_card(v, today) for v in venues)}</div>'
            '<div class="empty" id="f-empty" hidden><p>No matching places.</p>'
            '<button type="button" class="btn btn-d" data-reset>Clear search</button></div>' + safety)


def evidence_status(v, value):
    """Legacy public evidence only, following twin's fact/source_claim/unverified boundary.

    A venue-wide verification badge does not verify every field. Private twin stores
    are deliberately not imported: only the operations-owned published fields are used.
    """
    if not value or re.search(r"not confirmed|not verified|not yet|pending|unconfirmed|unverified|not stated|not announced|not listed|conflict|확인 중", value, re.I):
        return "unverified"
    if re.search(r"third.party|blog|roundup|source claim|not official|venue reply|hotel reply", value, re.I):
        # A future reply/booking procedure is not a received venue reply.
        if not re.search(r"awaiting official hotel reply|after an email reply|official page and hotel reply", value, re.I):
            return "source_claim"
    url = (v.get("source") or "").lower()
    if any(token in url for token in ("realtime.seoul", "blog.", "hospitalk.net", "crossfit.com/gym/", "hyrox.com/")):
        return "source_claim"
    # Keep the CSV's verification provenance: a plausible URL is not verification.
    # As in legacy intake, an official-page confidence marker is positive evidence;
    # an explicit non-official/unreachable marker always takes precedence.
    confidence = v.get("confidence", v.get("status", "")).strip().lower()
    official = confidence == "verified" or (
        confidence.startswith("high") and "official" in confidence and
        not re.search(r"not official|third.party|roundup|unreachable|licence record only|instagram|operator-run", confidence))
    if url and official:
        return "fact"
    return "source_claim" if url else "unverified"


def display_value(v, key, value):
    # Third-party prices and hours never become public copy, even as clues.
    if re.search(r"third.party|unverified clue|blog", value, re.I) and (
            re.search(r"price|hours|closed|day pass", key, re.I) or "₩" in value or
            re.search(r"\b24\s*(?:h\b|hours)|\b\d{1,2}:\d{2}\b", value, re.I)):
        return "Unconfirmed (official price / hours unavailable)"
    return value or UNKNOWN


def amenity_labels(v):
    """Small, conservative badges from positive own-source statements only."""
    rows = [(k.lower(), val.lower()) for k, val in v["facts"] if evidence_status(v, val) == "fact"]
    # Reject the whole ambiguous/negative row instead of guessing a positive clause.
    rows = [(k, val) for k, val in rows if not re.search(r"\bno\b|\bnot\b|unavailable|except|without", val)]
    labels = []
    def has(pattern): return any(re.search(pattern, k + " " + val) for k, val in rows)
    if v["kind_key"] == "sauna":
        def cold_plunge(key, value):
            # Bind the temperature to the bath, not a sauna or a cold shower.
            text = key + ": " + value
            if "급냉탕" in text: return True
            matches = re.findall(r"cold (?:bath|plunge|pool)(?: temperature)?[\s:=(]*(\d+(?:\.\d+)?)(?:\s*[~–-]\s*(\d+(?:\.\d+)?))?\s*(?:°\s*c|℃)", text)
            return bool(matches) and all(float(t) <= 15 for pair in matches for t in pair if t)
        cold = any(cold_plunge(k, val) for k, val in rows)
        if cold: labels.append("Great cold plunge")
        if has(r"dry (?:and wet )?sauna|wet and dry sauna|건식"): labels.append("Dry sauna")
        if has(r"wet (?:and dry )?sauna|dry and wet (?:\(steam\) )?sauna|steam sauna|습식"): labels.append("Wet/Steam")
        if has(r"private sauna|one.person sauna|sauna for one"): labels.append("Private")
    else:
        if has(r"\bshowers?\b"): labels.append("Showers")
        if has(r"\bparking\b"): labels.append("Parking")
        if has(r"drop.in.*(?:welcome|available|book|₩)|(?:welcome|book).*drop.in"): labels.append("Drop-in OK")
        if has(r"english.speaking coach|coach.*speaks english|english coach"): labels.append("English coach")
    return labels


def amenity_badges(v, limit=None):
    labels = amenity_labels(v)
    return ('<div class="amenity-badges" aria-label="Verified amenities">' +
            "".join(badge(label) for label in labels[:limit]) + '</div>') if labels else ""


def venue_image_path(v):
    """Consume optional assets from the separate image job; never generate images."""
    for extension in ("webp", "png"):
        path = VENUE_SKETCH / f'{v["slug"]}.{extension}'
        if path.is_file(): return path
    return None


def venue_media(v):
    path = venue_image_path(v)
    if path:
        return (f'<img class="card-image venue-image" src="/assets/venue-sketch/{E(path.name)}" '
                f'width="800" height="600" loading="lazy" decoding="async" alt="{E(v["title"])}">')
    return '<div class="card-image card-placeholder" aria-hidden="true"></div>'


class Site:
    def __init__(self, today, release):
        self.today, self.release = today, release
        self.pages = {}  # path -> html
        self.noindex = set()  # pages kept out of canonical/sitemap even in release (e.g. /join/welcome/)
        self.creators = False
        self.feature = None

    def head(self, title, desc, path, noindex=False):
        full = title if title.startswith(BRAND_EN) else f"{title} · {BRAND_EN}"
        indexable = self.release and not noindex
        robots = "" if indexable else '<meta name="robots" content="noindex,nofollow">\n'
        canon = f'<link rel="canonical" href="{SITE_URL}{path}">\n' if indexable else ""
        og = (f'<meta property="og:type" content="website"><meta property="og:site_name" content="{BRAND_EN}">'
              f'<meta property="og:title" content="{E(full)}"><meta property="og:description" content="{E(desc)}">'
              + (f'<meta property="og:url" content="{SITE_URL}{path}"><meta property="og:locale" content="en_US">'
                 f'<meta property="og:image" content="{SITE_URL}/assets/img/og-image.png"><meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">'
                 f'<meta property="og:image:alt" content="HANSET - hand-drawn floor plan of a CrossFit box">'
                 f'<meta name="twitter:card" content="summary_large_image">' if indexable else ""))
        return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<title>{E(full)}</title><meta name="description" content="{E(desc)}">\n{robots}{canon}{og}'
                f'<meta name="msvalidate.01" content="5986AEB0A3C1E9F1E558DA5CA02E1CE0" /><meta name="theme-color" content="#F5F2EC"><link rel="icon" href="/favicon.svg" type="image/svg+xml">'
                f'<link rel="preload" href="/assets/fonts/Pretendard-Regular.subset.woff2" as="font" type="font/woff2" crossorigin>'
                f'<link rel="preload" href="/assets/fonts/Excalifont/Excalifont-Regular.woff2" as="font" type="font/woff2" crossorigin>'
                f'<link rel="stylesheet" href="/assets/css/site.css?v={asset_ver("css/site.css")}"></head>')

    def nav(self, active):
        tabs = "".join(f'<a href="/{key}/"' + (' aria-current="page"' if active == key else '')
                       + f'>{tab_icon(key)}<span>{label}</span></a>' for key, label in TABS)
        return ('<a class="skip" href="#main">Skip to content</a><header class="site-header"><div class="wrap">'
                f'<a class="brand" href="/">{BRAND_EN}</a><nav class="section-tabs" aria-label="Explore">{tabs}</nav>'
                '</div></header>')

    def foot(self, active):
        feature_link = '<a href="/hyrox-seoul/">Event guide</a>' if self.feature else ""
        return (f'<footer class="site-footer"><div class="wrap"><strong class="site-title">{BRAND_EN}</strong> · Train. Sweat. One set.'
                f'<div class="flinks"><a href="/finder/">Finder</a><a href="/join/">Join</a><a href="/about/">About</a><a href="/guide/">Guide</a><a href="/archive/">Venue Sketches</a><a href="/events/">Events</a>{CREATORS_LINK if self.creators else ""}{feature_link}<a href="/faq/">FAQ</a><a href="/corrections/">Corrections</a>'
                f'<a href="/privacy/">Privacy</a></div>'
                f'<p class="fsocial"><a class="ig ig-f" {IG_ATTRS}>{IG_SVG}<span>@hanset.mag</span></a></p>'
                f'<p>Prices and closing days change. Every entry shows its check date and source. Confirm with the venue before you go.</p>'
                f'<p>© 2026 {BRAND_EN}</p></div></footer>')

    def add(self, path, title, desc, body, active="", scripts=(), noindex=False):
        if noindex: self.noindex.add(path)
        sc = "".join(f'<script src="/assets/js/{s}?v={asset_ver("js/"+s)}" defer></script>' for s in scripts)
        out_path = path
        self.pages[out_path] = (self.head(title, desc, path, noindex)
            + (f'<body class="home"><a class="skip" href="#main">Skip to content</a><main id="main" class="wrap">{body}</main>'
               '<footer class="home-footer"><a href="mailto:hanset@soulbound.co.kr">hanset@soulbound.co.kr</a></footer>'
               if active == "home" else f'<body>{self.nav(active)}<main id="main" class="wrap">{body}</main>{self.foot(active)}')
            + f'{sc}</body></html>')

def clip(t, n=160):
    """Meta description: never cut inside a word or number; prefer a sentence end."""
    if len(t) <= n: return t
    cut = t[:n]
    end = cut.rfind(". ")
    return cut[:end + 1] if end > n // 2 else cut[:cut.rfind(" ")].rstrip(" ,·—(") + "…"
def badge(txt, cls=""): return f'<span class="badge {cls}">{E(txt)}</span>'
def v_badges(v, today, full=False):
    b = []
    b.append(badge("Box" if v["kind_key"] == "box" else "Sauna", "badge-box" if v["kind_key"] == "box" else "badge-sauna"))
    b.append(badge(v["kind"]))
    if v["c"].get("opening_soon"): b.append(badge("Opening soon", "badge-part"))
    b.append(freshness(v["last_verified"], today))
    return '<div class="chips">' + "".join(b) + "</div>"
def off_html(v, today):
    o = v["off"]
    raw = o.get("date") or ""
    checked = parse_evidence_date(raw)
    checked_text = raw if checked else f'{raw} (date not confirmed)'.strip()
    return "".join(f'<div class="off">✓ {E(label)} · Checked {E(checked_text)} · '
                   f'<a href="{E(o[key])}" rel="noopener noreferrer">Official list</a> {freshness(checked, today)}</div>'
                   for key, label in (("cf", "CrossFit affiliate verified"), ("hyrox", "HYROX official gym list verified")) if o[key])
SUN_TXT = {"yes": "Open Sunday (verified)", "no": "Closed Sunday (verified)", "unk": "Sunday: not confirmed"}

def cav(v): return f'<div class="meta cav">※ {E(v["price_caveat"])}</div>' if v["price_caveat"] and v["price"] else ""

def map_url(v):
    return "https://map.naver.com/p/search/" + quote(v["title_ko"] + (" " + v["address"] if v["address"] else ""))

UNKNOWN = PENDING


def access_conflict(v):
    """Keep conflicting page/reply access claims unresolved, without picking a winner."""
    rows = [(k.lower(), val.lower()) for k, val in v["facts"]]
    denied = any(k in ("who can use", "non-guests", "entry") and
                 any(t in val for t in ("no outside guests", "no non-guests", "guests only"))
                 for k, val in rows)
    reply_access = any(k == "non-guests" and "reply" in val and
                       any(t in val for t in ("coupon", "welcome", "open to")) for k, val in rows)
    return denied and reply_access


def fact_groups(v):
    """Map original conditions verbatim. Never merge a checklist over source facts."""
    facts = [(k, val) for k, val in v["facts"] if val.strip()]
    visitor = FOREIGNER.get(v["slug"], {})
    hours, access, rules = [], [], []
    for k, val in facts:
        key = k.lower()
        if (key.startswith("hours") or key in ("class times", "saturday", "sunday", "closed", "closed / adjusted",
                "timetable", "temporary closures", "public holidays", "hybrid class", "citizens' forest branch")
                or (key == "open gym" and "₩" not in val)):
            hours.append((k, val))
        elif key in ("who can use", "booking", "request", "entry", "non-guests", "capacity",
                     "session length", "time allowed", "types · length", "hotel guests", "language"):
            access.append((k, val))
        elif key in ("conditions", "house rules", "open-air bath rules", "age", "note", "status", "body scrub"):
            rules.append((k, val))
    if not any(k == "Sunday" for k, _ in hours) and (v["kind_key"] == "box" or v["sunday"] != "unk"):
        hours.append(("Sunday", SUN_TXT[v["sunday"]] if v["sunday"] != "unk" else UNKNOWN))
    # Restrictions can be in tips rather than the facts table. Keep the whole tip.
    for tip in v.get("tips", []):
        if re.search(r"tattoo|children|guardian|age[s]?\b|under \d|no pairs|can't share|one person", tip, re.I):
            if not any(tip == value for _, value in rules + access):
                rules.append(("Entry restriction", tip))
    tattoo = [(k, val) for k, val in facts + rules if "tattoo" in val.lower() or "tattoo" in k.lower()]
    rules.insert(0, ("Tattoos", tattoo[0][1] if tattoo else visitor.get("tattoo") or UNKNOWN))
    access_facts = [(k, val) for k, val in access if k in ("Who can use", "Non-guests", "Entry")]
    if access_conflict(v):
        access.insert(0, ("Visitor access", UNKNOWN + " · Official page and venue reply differ; confirm with the venue."))
    elif not access_facts:
        visitor_access = visitor.get("dropin") or UNKNOWN
        if re.search(r"pending|not stated|not confirmed", visitor_access, re.I):
            visitor_access = UNKNOWN
        access.insert(0, ("Visitor access", visitor_access))
    elif (all(re.search(r"pending|not stated|not confirmed", val, re.I) for _, val in access_facts)
          or re.search(r"pending|not stated|not confirmed", visitor.get("dropin", ""), re.I)):
        access.insert(0, ("Visitor access", UNKNOWN))
    if v["kind_key"] == "sauna":
        for key, label in SAUNA_PKG_KEYS:
            if key not in visitor:
                continue
            value = visitor[key] if _is_confirmed(visitor[key]) else UNKNOWN
            if key in ("last_entry", "closures"):
                group = hours
            elif key in ("scrub_fee", "age_tattoo_attire"):
                group = rules
            else:
                group = access
            if (label, value) not in group:
                group.append((label, value))
    if visitor.get("english") and not (v["kind_key"] == "sauna" and "english" in visitor) and not any(k == "Language" for k, _ in access):
        access.append(("English support", visitor["english"]))
    facilities = [(k, val) for k, val in facts if re.search(r"facilit|temperature|sauna|bath|amenit|equipment|floors|areas|sequence|concept", k, re.I)
                  and (k, val) not in hours + access + rules]
    programs = [(k, val) for k, val in facts if re.search(r"program|classes|beginner|hybrid|training", k, re.I)
                and (k, val) not in hours + access + rules + facilities]
    if v["kind_key"] == "sauna":
        # No measurements are inferred from a drawing, marketing summary or venue type.
        confirmed_facilities = [(k, val) for k, val in facilities if evidence_status(v, val) == "fact"]
        for label, pattern in (("Sauna temperature", r"sauna(?: temperature)?[\s:=(]*\d+\s*[°℃]"),
                               ("Cold bath temperature", r"cold (?:bath|plunge|pool)(?: temperature)?[\s:=(]*\d+\s*[°℃]"),
                               ("Cold plunge", r"cold (?:bath|plunge|pool)"),
                               ("Hot / warm baths", r"(?:hot|warm) (?:bath|pool)")):
            if not any(re.search(pattern, k + " " + val, re.I) for k, val in confirmed_facilities):
                facilities.append((label, UNKNOWN))
    return [("Hours & Closed", hours), ("Access / Booking", access), ("Visitor / Tattoo rules", rules),
            ("Facilities & temperatures", facilities), ("Programs", programs or [("Programs", UNKNOWN)])]


EVIDENCE_DATE = r"\b(?:\d{4}[-.]\d{2}[-.]\d{2}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}(?!/\d))\b"

def parse_evidence_date(value):
    """Yearless, invalid and ambiguous dates never acquire an invented check date."""
    if not isinstance(value, str): return None
    try:
        if re.fullmatch(r"\d{4}[-.]\d{2}[-.]\d{2}", value):
            return ymd(value.replace(".", "-"))
        for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"):
            try: return dt.datetime.strptime(value, fmt).date()
            except ValueError: pass
    except ValueError:
        pass
    return None

def citation_date(citations, marker):
    # Only dates immediately attached to the evidence marker count. Inquiries,
    # scheduled closures and other dates elsewhere in the prose are not evidence.
    dates = []
    for citation in citations:
        for match in re.finditer(marker + r"\s*[:,]?\s*(?:(?:on|dated|received)\s+)?(" + EVIDENCE_DATE + r")"
                                 r"((?:\s*(?:/|,|and|or|–|to)\s*" + EVIDENCE_DATE + r")*)", citation, re.I):
            clause = re.split(r"[();\n]|—|--", citation[:match.start()])[-1]
            if re.search(r"\binquiry\b", clause, re.I): continue
            dates.extend(re.findall(EVIDENCE_DATE, match[1] + match[2], re.I))
    dates = list(dict.fromkeys(dates))
    parsed = [parse_evidence_date(date) for date in dates]
    checked = parsed[0] if parsed and parsed[0] and all(date == parsed[0] for date in parsed) else None
    text = ", ".join(dates) if dates else "date not confirmed"
    if dates and checked is None: text += " (date not confirmed)"
    return text, checked

def fact_provenance(v, value, today, *, link_claims=False):
    """A venue reply is not evidence available at the representative website."""
    if not value or value.startswith(UNKNOWN) or value.lower() in ("pending", "not confirmed", "not yet announced"):
        return ""
    citations = re.findall(
        r"\((?:official page and )?(?:venue|hotel) reply\b[^()]*\)"
        r"|(?:—|--)\s*(?:venue|hotel) reply\b[^\n()]*", value, re.I)
    if citations:
        # Dates outside an explicit reply citation may be closures or inquiry dates.
        checked_text, checked = citation_date(citations, r"(?:venue|hotel) reply\b")
        if checked_text != "date not confirmed" or not any(c.lower().startswith("(official page and ") for c in citations):
            status = "Unconfirmed" if evidence_status(v, value) == "unverified" else "Source claim"
            return (f'<small class="fact-source">Venue reply · {E(checked_text)} · {status} · '
                    f'Public source: {UNKNOWN} {freshness(checked, today)}</small>')
    status = evidence_status(v, value)
    label = {"fact": "Official source", "source_claim": "Source claim", "unverified": "Unconfirmed"}[status]
    # An explicit "checked" marker claims independent verification, so its date is the
    # evidence date; a missing/ambiguous one stays unconfirmed rather than inheriting the
    # venue's age. Prose that merely contains "checked" without a date is treated the same
    # way — published facts always pair the marker with a date (see the citation tests).
    if re.search(r"\bchecked\b", value, re.I):
        checked_text, checked = citation_date([value], r"\bchecked\b")
    else:
        checked, checked_text = v["last_verified"], dot(v["last_verified"])
    source = (f'<a href="{E(v["source"])}" rel="noopener noreferrer">{label} · {E(host(v["source"]))}</a>'
              if status == "fact" or (link_claims and status == "source_claim" and v["source"])
              else f'{label} · {E(host(v["source"]))}')
    return (f'<small class="fact-source">Checked {E(checked_text)} · '
            f'{source} {freshness(checked, today)}</small>')


def fact_lines(rows, v, today):
    return "".join(f'<span class="fact-line"><strong>{E(k)}:</strong> {E(display_value(v, k, val) if v else val)}'
                   f'{fact_provenance(v, display_value(v, k, val), today) if v else ""}</span>' for k, val in rows) or f'<span class="unk">Unconfirmed</span>'


def price_numeric_provenance(v, context, today):
    # The numeric is a sortable hint summarizing the price terms, so it inherits the
    # evidence of the term(s) that actually state the amount — never the venue-wide
    # badge. A reply / source-claim price must not be upgraded to "Official source"
    # (rule 7); its dated evidence stays on the terms below. An official amount keeps
    # its official provenance even when a sibling term (e.g. a reply discount) does not.
    matched = [val for _, val in context if v["price"] in val]
    if not matched:
        return ""
    rank = {"unverified": 0, "source_claim": 1, "fact": 2}
    weakest = min(matched, key=lambda val: rank[evidence_status(v, val)])
    if evidence_status(v, weakest) != "fact":
        return ""
    return fact_provenance(v, weakest, today)


def price_html(v, today):
    # The numeric CSV value is a sortable hint, not a replacement for the terms.
    context = [(k, val) for k, val in v["facts"] if
               (k.startswith("Price") or k.startswith("Drop-in") or k in
                ("Day pass", "Group session", "Open gym", "Traveler pass", "Trial", "Guest discount rate", "Jjimjil", "Jjimjil wear"))
               and val.strip() and val != PENDING]
    numeric = (f'<div class="price"><span class="price-label">{E(v["price_label"])}:</span> '
               f'<strong>{E(v["price"])}</strong></div>{cav(v)}{price_numeric_provenance(v, context, today)}') if v["price"] else ""
    terms = fact_lines(context, v, today) if context else ("" if numeric else f'<span class="unk">Price: {UNKNOWN}</span>')
    return numeric + '<div class="price price-context">' + terms + '</div>'


def price_short(v):
    # One visible line on cards ('₩49,000~'); the full price-plan label and terms stay in the
    # accessible name and tooltip (a class price is not a day pass) and in full on the detail page.
    if not v["price"]: return ""
    full = f'{v["price_label"]} {v["price"]}'
    m = re.search(r"₩\s?[\d,]+", v["price"])
    short = (m.group(0).replace(" ", "") + "~") if m else v["price"]
    return (f'<span class="price-short" title="{E(full)}"><span aria-hidden="true">{E(short)}</span>'
            f'<span class="vh">{E(full)}</span></span>')

def venue_facts(v, today):
    groups = []
    for label, rows in fact_groups(v):
        essential, extra = [], []
        for key, value in rows:
            # Keep access, tattoo, age, capacity and time limits visible in full.
            target = extra if (label == "Hours & Closed" and len(value) > 160) or key in (
                "House rules", "Open-air bath rules", "Body scrub", "English support", "Language") else essential
            if access_conflict(v) and key in ("Who can use", "Non-guests", "Entry"):
                target = extra
            if key == "Entry restriction" and any(value == val for k, val in essential):
                continue
            if key != "Tattoos" and any(value == val for k, val in essential if k == "Tattoos"):
                continue
            target.append((key, value))
        shown = fact_lines(essential, v, today) if essential else ""
        if extra:
            shown += f'<details class="fact-extra"><summary>Full {E(label.lower())} · source notes</summary>{fact_lines(extra, v, today)}</details>'
        groups.append(f'<div><dt>{E(label)}</dt><dd>{shown or fact_lines([], v, today)}</dd></div>')
    return '<dl class="card-facts">' + "".join(groups) + '</dl>'


HANGUL = re.compile(r"[\u3131-\u318e\uac00-\ud7a3]")

def en_only(s):
    """Visitor-facing English: drop Hangul. Addresses keep only the verified English rendering; '' if none."""
    if not s or not HANGUL.search(s): return s
    lead = s[:HANGUL.search(s).start()]
    lead = lead if lead.rstrip().endswith("·") else ""
    parens = [m for m in re.finditer(r"\(([^()]*)\)", s) if not HANGUL.search(m.group(1)) and re.search(r"\d", m.group(1))]
    if parens:  # address: English rendering given in parentheses (+ any English tail)
        m = max(parens, key=lambda p: len(p.group(1))); tail = s[m.end():]
        out = lead + m.group(1) + ("" if HANGUL.search(tail) else tail)
    else:
        out = re.sub(r"\s*\([^()]*[\u3131-\u318e\uac00-\ud7a3][^()]*\)", "", s)
        out = " ".join(w for w in out.split() if not HANGUL.search(w))
    out = re.sub(r"\s+", " ", out).strip(" ·,")
    return out if re.search(r"[A-Za-z]", out) else ""

def en_fact(k, v, loc=""):
    if k in ("Address", "Location", "Name & address"): return en_only(v) or (loc if v.strip() else v)
    return re.sub(r"\s*\([\u3131-\u318e\uac00-\ud7a3\s]+\)", "", v)  # e.g. "Body scrub (세신)"

SKETCH_DIR = ROOT / "static/venue-sketch"

def venue_sketch(v, cls="venue-sketch", lazy=True):
    """Hand-drawn venue illustration at static/venue-sketch/<venue_id>.(webp|svg|png); '' if none."""
    for ext in ("webp", "svg", "png"):
        if (SKETCH_DIR / f'{v["slug"]}.{ext}').exists():
            return (f'<img class="{cls}" src="/assets/venue-sketch/{v["slug"]}.{ext}" width="1200" height="900"'
                    f'{" loading=\"lazy\"" if lazy else ""} decoding="async" alt="Hand-drawn illustration of {E(v["title"])}">')
    return ""

def venue_contents(v, today):
    return (f'<a class="card-media" href="/finder/{v["slug"]}/" tabindex="-1" aria-hidden="true">{venue_media(v)}</a>'
            f'<div class="card-body"><h3><a href="/finder/{v["slug"]}/">{E(v["title"])}</a></h3>'
            f'<p class="card-type">{E(v["kind"])}</p><p class="card-meta">{E(v["area"] or v["location"])}'
            f'{(" · " + price_short(v)) if v["price"] else ""}</p>{amenity_badges(v, limit=3)}'
            f'<p class="card-evidence">{fact_provenance(v, "Venue information", today, link_claims=True)}</p></div>')

def venue_card(v, today):
    attrs = (f'data-slug="{v["slug"]}" data-city="{v["city_key"]}" data-type="{v["kind_key"]}" data-sunday="{v["sunday"]}" data-price="{1 if v["price"] else 0}" data-cf="{1 if v["off"]["cf"] else 0}" data-hyrox="{1 if v["off"]["hyrox"] else 0}" data-sub="{v["sub"]}" data-order="{v["order"]}" data-date="{v["last_verified"].isoformat()}" data-title="{E(v["title"])}" '
             f'data-name="{E((v["title"] + " " + (v.get("name") or v["title"]) + " " + v["location"] + " " + v["area"] + " " + v["kind"] + " " + v["title_ko"] + " " + v["location_ko"]).lower())}"')
    return f'<article class="card venue-card" {attrs}>{venue_contents(v, today)}</article>'

def mini_card(v, today):
    return f'<article class="card venue-card">{venue_contents(v, today)}</article>'

def event_lifecycle(e, today):
    """Calendar dates supplied by the builder are in Asia/Seoul; end is inclusive."""
    if today > e["end"]: return "ended", "Ended"
    if today < e["start"]: return "upcoming", f'D-{(e["start"] - today).days}'
    return "ongoing", "D-DAY" if today == e["start"] else "Live now"

def event_attrs(e, today):
    state, _ = event_lifecycle(e, today)
    return f'data-event-lifecycle="{state}" data-start="{e["start"].isoformat()}" data-end="{e["end"].isoformat()}"'

def event_card(e, today, detail_link=True):
    _, status = event_lifecycle(e, today)
    rows = "".join(f'<tr><th>{E(k)}</th><td>{E(val)}</td></tr>' for k, val in e["facts"])
    link = '<a href="/hyrox-seoul/">Event guide →</a> · ' if e.get("feature") and detail_link else ""
    return (f'<article class="card" id="{e["slug"]}" data-event-card {event_attrs(e, today)}><div class="chips">{badge(e["kind"])}</div>'
            f'<h3>{E(e["title"])}</h3><div class="when">{span(e["start"], e["end"], today.year)} · <b data-event-status>{status}</b></div>'
            f'<table class="facts"><tbody>{rows}</tbody></table>' + (f'<p class="muted">{E(e["note"])}</p>' if e["note"] else "") +
            f'<div class="meta">{link}<a href="{E(e["url"])}" rel="noopener noreferrer">{E(e["source_label"])}</a> · Checked {dot(e["last_verified"])} {freshness(e["last_verified"], today)}</div></article>')

# ------------------------------------------------------------------ pages
HYROX_STATIONS = [("SkiErg", "1,000m"), ("Sled Push", "50m"), ("Sled Pull", "50m"),
                  ("Burpee Broad Jump", "80m"), ("Rowing", "1,000m"), ("Farmers Carry", "200m"),
                  ("Sandbag Lunges", "100m"), ("Wall Balls", "100 reps")]

def feature_metadata(e, ended):
    dates = span(e["start"], e["end"], None)  # Always include years, including on cached pages.
    title = f'{e["title"]} · {"Past event" if ended else "Event guide"} ({dates}, {e["venue"]})'
    desc = f'{"Historical reference" if ended else "Event guide"}: {e["title"]}, {dates}, {e["venue"]}. Official dates, facts and sources.'
    return title, desc

def feature_body(feat, venues, today):
    ended = event_lifecycle(feat, today)[0] == "ended"
    _, status = event_lifecycle(feat, today)
    def copy(tag, active, past, attrs=""):
        return (f'<{tag} {attrs} data-event-active-text="{E(active)}" data-event-ended-text="{E(past)}">'
                f'{E(past if ended else active)}</{tag}>')
    metadata = " ".join(f'data-{state}-{key}="{E(value)}"'
                        for state, past in (("active", False), ("ended", True))
                        for key, value in zip(("title", "description"), feature_metadata(feat, past)))
    trs = "".join(f"<tr><td>{i}</td><td>1km</td><td>{E(en)}</td><td>{E(d)}</td></tr>" for i, (en, d) in enumerate(HYROX_STATIONS, 1))
    hv = "".join(mini_card(v, today) for v in venues)
    return f'''<section class="hero" data-event-page {event_attrs(feat, today)} {metadata}>
{copy("span", "EVENT GUIDE", "PAST EVENT", 'class="eyebrow"')}
{copy("h1", feat["title"], "Past event: " + feat["title"])}
<div class="dday"><b data-event-status>{status}</b>{span(feat["start"], feat["end"], None)} · {E(feat["venue"])}</div>
<p data-event-ended-only{'' if ended else ' hidden'}><strong>Past event</strong> · These scheduled dates have ended. This page is a historical reference; it does not confirm event outcomes.</p>
<p>Dates and event information from the official page.</p>
<div class="btnrow"><a class="btn btn-a" href="{E(feat["url"])}" rel="noopener noreferrer">Official event page</a></div></section>
<div class="page"><h2>At a glance</h2>
<table class="facts"><tbody>{"".join(f"<tr><th>{E(k)}</th><td>{E(val)}</td></tr>" for k, val in feat["facts"])}</tbody></table>
<h2>Race format reference</h2>
<p>1km run, one functional station. One set. Eight times. Order and distances below.</p>
<table class="t"><thead><tr><th>#</th><th>Run</th><th>Station</th><th>Distance · reps</th></tr></thead><tbody>{trs}</tbody></table>
<h2>Divisions</h2>
<ul><li>Open / Pro (solo)</li><li>Doubles / Doubles Pro (pairs)</li><li>Relay: teams of 4, each runs 2×1km and one station</li></ul>
<div class="note">Weights by division and gender are on the official page. Standards can change by season; see the <a href="https://hyrox.com/the-fitness-race/" rel="noopener noreferrer">official HYROX guide</a>. Registration status and fees weren’t in the official page text, so they're not listed.</div>
<h2>HYROX training reference</h2><p>Places with HYROX classes or drop-ins confirmed on their official pages. Prices only when verified.</p></div>
<div class="grid g3">{hv}</div>
<div class="page"><p class="muted">Find verified saunas and boxes in the <a href="/finder/">Finder</a>.</p>
<p class="muted">{SAFETY}</p><p class="muted">Checked {dot(feat["last_verified"])} {freshness(feat["last_verified"], today)} · Source <a href="{E(feat["url"])}" rel="noopener noreferrer">{E(feat["source_label"])}</a> · <a href="https://hyrox.com/the-fitness-race/" rel="noopener noreferrer">HYROX official</a></p></div>'''


# ------------------------------------------------------------------ magazine (articles)
ARTICLES = [  # newest first; the Magazine index remains available when empty.
    # 2026-10-20: old Wellness Report removed (archived in content/articles/_archive/). New English articles go here.
    {"slug": "bryan-johnson-sauna-protocol", "file": "bryan-johnson-sauna-protocol.md", "date": "2026-10-22", "category": "Sauna", "lang": "en",
     "title_en": "Bryan Johnson's Sauna Protocol, Read Closely", "deck_en": "Hot room, ice on the face. Here is what he says, and what the research says."},
]

def parse_article(meta):
    raw = (ROOT / "content/articles" / meta["file"]).read_text(encoding="utf-8")
    title = re.search(r"^# (.+)$", raw, re.M).group(1).strip()
    secs, cur = {}, None
    for line in raw.splitlines()[1:]:
        m = re.match(r"^## (.+)$", line)
        if m: cur = m.group(1).strip(); secs[cur] = []; continue
        if cur is not None: secs[cur].append(line)
    for k in secs: secs[k] = "\n".join(secs[k]).strip()
    keep = ("데크", "본문", "3줄 요약", "출처", "안내 문구")  # 자체 점검 and the video script are intentionally dropped
    return dict(meta, title=title, **{k: secs[k] for k in keep})

def cite(text):
    def sub(m):
        nums = [n.strip() for n in m.group(1).split(",")]
        return "(" + ", ".join(f'<a class="cite" href="#ref-{n}">{n}</a>' for n in nums) + ")"
    return re.sub(r"\((\d+(?:,\s*\d+)*)\)", sub, E(text))

BYLINE = 'Editor Cheon Han-saem'

def article_html(a):
    d = dt.date.fromisoformat(a["date"])
    parts = []
    for blk in re.split(r"\n\s*\n", a["본문"]):
        blk = blk.strip()
        if not blk: continue
        m = re.match(r"^Original post: (https://x\.com/\S+)$", blk)
        if m: parts.append(f'<p>Original post: <a href="{E(m.group(1))}" rel="noopener noreferrer">{E(m.group(1).split("//")[1])}</a></p>'); continue
        parts.append(f'<h2>{E(blk[4:].strip())}</h2>' if blk.startswith("### ") else f"<p>{cite(blk)}</p>")
    summ = "".join(f"<li>{cite(re.sub(r'^\d+\.\s*', '', l.strip()))}</li>" for l in a["3줄 요약"].splitlines() if l.strip())
    refs = []
    for l in a["출처"].splitlines():
        m = re.match(r"^(\d+)\.\s*(.+)$", l.strip())
        if not m: continue
        n, t = m.group(1), m.group(2)
        doi = re.search(r"DOI\s+(10\.\S+?)(?=\s*(?:\(|$))", t)
        link = f' <a href="https://doi.org/{E(doi.group(1))}" rel="noopener noreferrer">doi.org/{E(doi.group(1))}</a>' if doi else ""
        txt = re.sub(r"\s*DOI\s+10\.\S+", "", t)
        txt = re.sub(r"\s*\((?:PMID[^)]*|PMCID[^)]*)\)", "", txt).strip()
        refs.append(f'<li id="ref-{n}" value="{n}">{E(txt)}{link}</li>')
    return (f'<article class="article"><header class="a-head"><span class="eyebrow">{E(a["category"])}</span><h1>{E(a["title_en"])}</h1>'
            f'<p class="deck">{E(a["deck_en"])}</p><div class="byline">{BYLINE} · {dot(d)}</div>'
            + ('</header><div lang="en">' if a.get("lang") == "en" else
               f'<p class="muted lang-note"><strong>Article in Korean.</strong> The full text below is in Korean.</p></header>'
               f'<div lang="ko"><p class="ko-title"><strong>{E(a["title"])}</strong></p>') +
            f'<aside class="summary" aria-label="In brief"><h2 lang="en">In brief</h2><ol>{summ}</ol></aside>'
            f'<div class="a-body">{"".join(parts)}</div>'
            f'<section class="refs"><h2 lang="en">Sources</h2><ol>{"".join(refs)}</ol></section>'
            f'<div class="note">{E(a["안내 문구"])}</div></div>'
            f'<p><a href="/magazine/">← All articles</a></p></article>')

def build(site, venues, events, today, vevents=(), creators=()):
    by_slug = {v["slug"]: v for v in venues}
    feat = next((e for e in events if e.get("feature")), None)
    site.feature = feat  # Set before any page/footer is rendered.
    upcoming = [e for e in events if e["end"] >= today]
    ordered = sorted(venues, key=lambda v: (not v["price"], v["status"] != "verified", -len(v["facts"]), v["order"]))
    cnt_box = sum(v["kind_key"] == "box" for v in venues); cnt_sauna = len(venues) - cnt_box
    seg = lambda g, items: '<div class="seg" data-seg="%s" role="group" aria-label="%s">' % (g, g.title()) + "".join(f'<button type="button" data-v="{k}" aria-pressed="false">{E(t)}</button>' for k, t in items) + "</div>"
    explorer = f'''
<form class="filters" aria-label="Filters">
 <div class="fgroup"><span>City</span>{seg("city", [("", "All"), ("seoul", "Seoul"), ("busan", "Busan"), ("jeju", "Jeju"), ("gyeonggi", "Gyeonggi")])}</div>
 <div class="fgroup"><span>Type</span>{seg("type", [("", "All"), ("box", "CrossFit · HYROX boxes"), ("sauna", "Saunas")])}</div>
 <div class="fgroup"><label for="f-q">Search name or area</label><input id="f-q" type="search" placeholder="e.g. Hannam, Busan, hotel" autocomplete="off"></div>
 <details class="more-filters"><summary>More filters <span id="f-more-count"></span></summary><div class="more-filter-fields">
 <div class="fgroup"><label for="f-sub">Sauna type</label><select id="f-sub"><option value="">All</option><option value="hotel">Hotel Saunas</option><option value="boutique">Boutique Saunas</option><option value="spa">Large Spa · Hot Spring</option></select></div>
 <div class="fgroup"><label for="f-sort">Sort</label><select id="f-sort"><option value="">Default</option><option value="date">Recently checked</option><option value="name">A–Z</option></select></div>
 <div class="fgroup"><label for="f-sunday">Sunday</label><select id="f-sunday"><option value="">All</option><option value="yes">Open (verified)</option><option value="no">Closed (verified)</option><option value="unk">Not confirmed</option></select></div>
 <div class="fgroup"><span>Price</span><label class="chk"><input id="f-price" type="checkbox"> Price confirmed only</label></div>
 <div class="fgroup"><span>Official lists</span><label class="chk"><input id="f-hyrox" type="checkbox"> HYROX gyms (official list)</label> <label class="chk"><input id="f-cf" type="checkbox"> CrossFit affiliates (verified)</label></div>
 <div class="fgroup"><span>&nbsp;</span><button id="f-reset" data-reset type="button" class="btn btn-d">Start over</button></div>
 </div></details>
</form>
<p class="count" id="f-count" aria-live="polite">{len(venues)} places</p>
<div class="grid" id="finder-list">{"".join(venue_card(v, today) for v in ordered)}</div>
<div class="empty" id="f-empty" hidden><p>Nothing here yet.</p><p class="muted">Try fewer filters. More verified places are on the way.</p><button type="button" class="btn btn-d" data-reset>Start over</button></div>
<div class="note">"Verified" means we read the info directly on the venue’s official page. "Partly verified" means only some items, like the address. Sunday is filled in only when the official page says so. "CrossFit affiliate verified" and "HYROX official gym list verified" appear only where we saw the place ourselves in that official page or list on the check date. No mark means still checking.</div>
<noscript><p class="muted">Filters need JavaScript. The full list still works without it.</p></noscript>'''
    # ---- home: search records stay hidden until a nonblank query.
    records = "".join(f'<li data-search="{E(search_text(v))}" hidden><a href="/finder/{v["slug"]}/">{E(v["title"])}</a>'
                      f'<span>{E(v["city"])} · {E(v["kind"])}</span></li>' for v in venues)
    illustrations = "".join(f'<li><a class="home-ill-link" href="/guide/#{k}">'
                            f'<img src="/assets/img/guide/{f}-thumb.png" width="252" height="315" loading="lazy" decoding="async" alt="{E(d)}">'
                            f'<span><b>{E(t)}</b><em>Open guide ›</em></span></a></li>' for k, f, t, d in ILLUSTRATIONS)
    body = f'''<div class="home-search"><h1>HANSET, Sweat on Korea</h1>
<form class="search-form" action="/finder/" role="search"><label class="vh" for="home-q">Where do you want to sweat in Korea?</label>
<input id="home-q" name="q" type="search" placeholder="Where do you want to sweat in Korea?" autocomplete="off" aria-controls="search-results"></form>
<div class="home-switch" role="group" aria-label="Browse"><a class="selected" href="/sauna/">Sweat</a><a href="/magazine/">Magazine</a></div>
<div id="search-results" hidden><p id="search-count" role="status" aria-live="polite"></p><ul class="search-records">{records}</ul></div></div>
<section class="home-illustrations" aria-labelledby="home-pictures"><h2 id="home-pictures">In pictures</h2>
<p>Hand-drawn guides for first-timers. Tap to open.</p><ul class="home-ill-list">{illustrations}</ul>
<p class="safety">{SAFETY}</p></section>'''
    site.add("/", "HANSET, Sweat on Korea", "Find saunas, CrossFit, HYROX and gyms in Korea.", body, "home", ("search.js",))

    # ---- category lists: shared classification, scoped URL-searchable cards.
    for key, label in TABS[:-1]:
        selected = [v for v in ordered if key in venue_categories(v)]
        site.add(f"/{key}/", label, f"Explore {label} in Korea.", category_body(key, label, selected, today), key, ("finder.js",))

    # ---- finder
    fbody = f'''
<div class="finder-head"><h1>{FINDER_LABEL}</h1>
<p class="lead"><b>{cnt_box}</b> boxes · <b>{cnt_sauna}</b> saunas</p></div>
<h2 class="sr-only">All venues</h2>
{explorer}'''
    site.add("/finder/", f"{FINDER_LABEL} — Korea", "CrossFit and HYROX boxes, hotel and boutique saunas in Korea. Filter by city, type, Sunday hours and confirmed prices. Every entry with a check date and source.", fbody, "finder", ("finder.js",))

    # ---- venue pages
    hyrox_v = [v for v in venues if v["hyrox"]]
    by_city = {}
    for v in venues:
        by_city.setdefault(v["city_key"], []).append(v)
    for city in by_city:
        by_city[city].sort(key=lambda v: v["order"])
    all_ordered = sorted(venues, key=lambda v: v["order"])
    for v in venues:
        def rows():
            out = []
            for k, val in v["facts"]:
                if k == "Operator": continue
                shown = display_value(v, k, val)
                cell = f'<td>{E(shown)}{fact_provenance(v, shown, today)}</td>'
                out.append(f'<tr><th>{E(k)}</th>{cell}</tr>')
            return "".join(out)
        links = []
        if v["website"] and "map.naver.com" not in v["website"]:
            links.append(f'<a class="btn btn-d" href="{E(v["website"])}" rel="noopener noreferrer">Official site</a>')
        if v["booking"] and v["booking"] != v["website"]:
            links.append(f'<a class="btn btn-d" href="{E(v["booking"])}" rel="noopener noreferrer">Book</a>')
        links.append(f'<a class="btn btn-d" href="{E(map_url(v))}" rel="noopener noreferrer">Open in map</a>')
        srcs = [f'<a href="{E(v["source"])}" rel="noopener noreferrer">{E(host(v["source"]))}</a>'] + [f'<a href="{E(u)}" rel="noopener noreferrer">{E(l)}</a>' for l, u in v["extra_sources"]]
        tips = ("<h2>Tips · Heads-up</h2><ul class='clean'>" + "".join(f"<li>{E(display_value(v, 'Tip', t))}{fact_provenance(v, display_value(v, 'Tip', t), today)}</li>" for t in v["tips"]) + "</ul>") if v["tips"] else ""
        unv = ("<h2>Unconfirmed</h2><ul class='clean'>" + "".join(f"<li>{E(display_value(v, 'Unconfirmed', t))}{fact_provenance(v, 'Unconfirmed: ' + display_value(v, 'Unconfirmed', t), today)}</li>" for t in v["unverified"]) + "</ul>") if v["unverified"] else ""
        stale = (f'<div class="note">{OUTDATED} · Last checked over {STALE_DAYS} days ago. Confirm with the venue before you go.</div>' if is_outdated(v["last_verified"], today) else "")
        safety = f'<p class="safety">{SAFETY}</p>' if v["kind_key"] == "sauna" else ""
        offrows = f'<tr><th>Official list</th><td>{off_html(v, today)}</td></tr>' if v["off"]["cf"] or v["off"]["hyrox"] else ""
        extra_addr = f'<tr><th>Address</th><td>{E(en_only(v["address"]))}</td></tr>' if en_only(v["address"] or "") and not any(k == "주소" for k, _ in v["facts_ko"]) else ""
        sunrow = f'<tr><th>Sunday</th><td{"" if v["sunday"] != "unk" else " class=unk"}>{SUN_TXT[v["sunday"]] if v["sunday"] != "unk" else PENDING}</td></tr>' if v["kind_key"] == "box" or v["sunday"] != "unk" else ""
        if v["kind_key"] == "box" and any("일요일" in k or "휴무" in k for k, _ in v["facts_ko"]) and v["sunday"] != "unk": sunrow = ""
        if v["kind_key"] == "box" and v["sunday"] == "unk" and any("일요일" in k for k, _ in v["facts_ko"]): sunrow = ""
        _ar = ARCHIVE_BY_VENUE.get(v["slug"])
        _ae = AERIAL_BY_VENUE.get(v["slug"])
        aer_blk = (f'<!--aerial--><section class="archbox aerbox" id="aerial"><h2>Bird’s-eye drawing</h2><a class="illc" href="/archive/#aerial-{v["slug"]}"><picture><img src="/assets/img/aerial/{_ae[0]}-thumb.webp" width="252" height="315" loading="lazy" decoding="async" alt="{E(_ae[2])}"></picture>'
                   f'<span><b>{E(v["title"])}</b><em>From above · Tap to view ›</em></span></a></section><!--/aerial-->') if _ae else ""
        arch_blk = (f'<section class="archbox"><h2>Hand-drawn sketch</h2><a class="illc" href="/archive/#{v["slug"]}"><picture><img src="/assets/img/archive/{_ar[0]}-thumb.png" width="252" height="315" loading="lazy" decoding="async" alt="{E(_ar[4])}"></picture>'
                    f'<span><b>{E(_ar[2])}</b><em>Tap to view ›</em></span></a></section>') if _ar else ""
        city_group = by_city.get(v["city_key"], [])
        same_city = len(city_group) > 1
        if same_city:
            idx = next((i for i, x in enumerate(city_group) if x["slug"] == v["slug"]), 0)
            nxt = city_group[(idx + 1) % len(city_group)]
        else:
            idx = next((i for i, x in enumerate(all_ordered) if x["slug"] == v["slug"]), 0)
            nxt = all_ordered[(idx + 1) % len(all_ordered)]
        next_label = f'Next in this city: {E(nxt["title"])} ›' if same_city else f'Next: {E(nxt["title"])} ›'
        next_html = f'<p class="next-venue"><a href="/finder/{nxt["slug"]}/">{next_label}</a></p>'
        body = f'''<div class="detail"><p class="crumb"><a href="/finder/">{FINDER_LABEL}</a> › {E(v["city_ko"])}</p>
{v_badges(v, today)}{amenity_badges(v)}<h1>{E(v["title"])}</h1><p class="muted">{E(v["location"])}</p>{stale}
<div class="detail-media">{venue_media(v)}</div>
<div class="btnrow detail-actions">{"".join(links)}</div>
<section class="detail-glance" aria-labelledby="glance-heading"><h2 id="glance-heading">At a glance</h2>
<div class="glance-grid"><div><h3>Price</h3>{price_html(v, today)}</div>{"".join(f'<div><h3>{E(label)}</h3>{fact_lines(values, v, today)}</div>' for label, values in fact_groups(v))}</div>
<p class="meta">Checked {dot(v["last_verified"])} {freshness(v["last_verified"], today)} · Reference <a href="{E(v["source"])}" rel="noopener noreferrer">{E(host(v["source"]))}</a></p></section>
<h2>Source notes</h2><table class="facts"><tbody>{rows()}{extra_addr}{sunrow}{closed_row(v, today)}{offrows}</tbody></table>
{tips}{unv}{fchk(v, today)}{aer_blk}{arch_blk}
<div class="verify"><p><strong>Last checked {dot(v["last_verified"])}</strong> {freshness(v["last_verified"], today)}</p><p>Source: {" · ".join(srcs)}</p>
<p><a href="/corrections/">Report a correction</a></p></div>
{next_html}
<div class="btnrow">{"".join(links)}</div>{safety}</div>'''
        site.add(f"/finder/{v['slug']}/", f"{v['title']}", clip(f"{v['title']} — {v['kind']} in {v['city']}. Sources, verification status and visitor information."), body,
                 next((key for key in ("sauna", "crossfit", "gym", "hyrox") if key in venue_categories(v)), ""))

    # ---- events
    cards = "".join(event_card(e, today, detail_link=e is feat) for e in upcoming)
    cards += ('<div class="empty" data-events-empty' + (' hidden' if upcoming else '')
              + '><p>No officially confirmed races right now.</p><p class="muted">We add them as they’re confirmed.</p></div>')
    ebody = f'''<div class="page"><h1>Event Calendar</h1></div>
<div class="ev">{cards}</div>
{vevents_section(vevents, today)}
<div class="note">Races listed only on aggregator sites wait until the organizer confirms. Fees appear only when read on the official page. <a href="/events.ics">Download all dates (.ics)</a></div>'''
    site.add("/events/", "Event Calendar", "Race dates confirmed on official pages.", ebody, "events", ("dday.js",))

    # ---- creators (verified feed; page only when there is data)
    if creators:
        cbody = (f'<div class="page"><h1>Creators</h1></div>'
                 f'<section class="sec vsec" aria-label="Creators"><div class="vlist">{"".join(creator_card(c, today) for c in creators)}</div>'
                 f'<p class="muted">Public profiles · dated</p></section>')
        site.add("/creators/", "Creators", "CrossFit, HYROX, running and sauna creators in Korea. Public profiles, checked by date.", cbody, "creators")

    # ---- optional feature (the historical route remains available after its end)
    if feat:
        title, desc = feature_metadata(feat, event_lifecycle(feat, today)[0] == "ended")
        site.add("/hyrox-seoul/", title, desc, feature_body(feat, hyrox_v, today), "", ("dday.js",))

    # ---- magazine
    arts = [parse_article(m) for m in ARTICLES]
    for a in arts:  # magazine pages only when there are articles
        site.add(f"/magazine/{a['slug']}/", a["title_en"], clip(a["deck_en"]), article_html(a), "magazine")
    mcards = "".join(f'<article class="card mag-card"><div class="chips">{badge(a["category"])}</div><h3><a href="/magazine/{a["slug"]}/">{E(a["title_en"])}</a></h3>'
                     f'<div class="loc">{E(a["deck_en"])}</div><div class="meta">{BYLINE} · {dot(dt.date.fromisoformat(a["date"]))}{"" if a.get("lang") == "en" else " · Article in Korean"}</div></article>' for a in arts)
    site.add("/magazine/", "Magazine", "Wellness reports from HANSET editors. Research and official sources, made easy to read.",
             f'<div class="page"><h1>Magazine</h1></div><h2 class="vh">Articles</h2><div class="grid">{mcards or "<p class=muted>Coming soon</p>"}</div>', "magazine")

    # ---- archive (손그림 아카이브)
    items = "".join(
        f'<article class="arc" id="{vs}"><a class="arcimg" href="/assets/img/archive/{f}.svg" target="_blank" rel="noopener" aria-label="{E(t)}: view larger (new tab)">'
        f'<picture><source media="(min-width:720px)" srcset="/assets/img/archive/{f}-mid.png"><img src="/assets/img/archive/{f}-thumb.png" width="252" height="315" loading="lazy" decoding="async" alt="{E(d)}"></picture></a>'
        f'<div class="arcbody"><h2>{E(t)}</h2>'
        f'<div class="btnrow"><a class="btn btn-d" href="/assets/img/archive/{f}.svg" target="_blank" rel="noopener">View larger ↗</a><a class="btn btn-d" href="/finder/{vs}/">Venue details</a></div></div></article>'
        for f, vs, t, st, d, sl, su in ARCHIVE)
    _vt = {a[1]: a[3] for a in ARCHIVE}
    aitems = "".join(
        f'<article class="arc" id="aerial-{vs}"><a class="arcimg" href="/assets/img/aerial/{f}.svg" target="_blank" rel="noopener" aria-label="{E(_vt.get(vs, vs))} bird’s-eye drawing: view larger (new tab)">'
        f'<picture><source media="(min-width:720px)" srcset="/assets/img/aerial/{f}-mid.webp"><img src="/assets/img/aerial/{f}-thumb.webp" width="252" height="315" loading="lazy" decoding="async" alt="{E(d)}"></picture></a>'
        f'<div class="arcbody"><h2>{E(_vt.get(vs, vs))}, from above</h2>'
        f'<div class="btnrow"><a class="btn btn-d" href="/assets/img/aerial/{f}.svg" target="_blank" rel="noopener">View larger ↗</a><a class="btn btn-d" href="/finder/{vs}/">Venue details</a></div></div></article>'
        for f, vs, d in AERIAL)
    abody = f'''<div class="page"><h1>Venue Sketches</h1>
<p class="muted">Tap to enlarge</p></div>
<!--aerial--><div id="aerial" class="aerwrap"><div class="page"><h2>Bird’s-eye drawings</h2><p class="lead">Each venue seen from above, drawn by hand.</p></div>
<div class="arcgrid aergrid">{aitems}</div></div><!--/aerial-->
<div class="page"><h2 id="sketches">Floor-by-floor sketches</h2></div>
<div class="arcgrid">{items}</div>
<div class="page"><p class="muted">{SAFETY}</p></div>'''
    site.add("/archive/", "Venue Sketches", "Hand-drawn sketches of floors and zones.", abody, "guide")

    # ---- static pages
    for slug, title, desc, active in [("guide", "Guide", "How to read HANSET's finder, badges and check dates, and what to know before you go.", "guide"),
                                      ("faq", "FAQ", "How HANSET verifies, what 'Pending' means, how to request a correction, and more.", "guide"),
                                      ("about", "About", "HANSET is a wellness magazine that lists CrossFit, HYROX, hotel and boutique saunas in Korea with official sources and check dates.", "about"),
                                       ("privacy", "Privacy", "HANSET runs without trackers.", "about"),
                                       ("corrections", "Corrections", "See something wrong? Tell us.", "about")]:
        frag = (ROOT / f"content/pages/{slug}.html").read_text(encoding="utf-8")
        if slug == "guide":
            feature_link = (f'<li><a href="/hyrox-seoul/">Event guide: {E(feat["title"])}</a>: dates and official event reference.</li>' if feat else "")
            frag = frag.replace("<!--FEATURE_LINK-->", feature_link)
            figs = "".join(f'<figure class="fig" id="{k}"><a class="figlink" href="/assets/img/guide/{f}.svg" target="_blank" rel="noopener" aria-label="{E(t)}: view larger (new tab)"><img src="/assets/img/guide/{f}.svg" width="1080" height="1350" loading="lazy" decoding="async" alt="{E(d)}"></a><figcaption><strong>{E(t)}</strong><br><a href="/assets/img/guide/{f}.svg" target="_blank" rel="noopener">View larger ↗</a></figcaption></figure>' for k, f, t, d in ILLUSTRATIONS)
            frag = frag.replace("<!--ILLUSTRATIONS-->", f'<h2 id="pictures">In pictures</h2><p>Hand-drawn guides for first-timers. Tap to enlarge.</p><div class="figs">{figs}</div>')
        site.add(f"/{slug}/", title, desc, f'<div class="page">{frag}</div>', active)

    # ---- join (Google sign-up; the OAuth flow runs in functions/api/auth/*, server-side only)
    jbody = '''<h1>Join HANSET.</h1>
<p class="lead">Weekly drop-ins and saunas, checked.</p>
<p class="join-error" id="join-error" hidden role="alert">Something went wrong. Try again.</p>
<form class="join-form" method="get" action="/api/auth/google">
<input type="hidden" name="nl" value="0">
<label class="chk"><input type="checkbox" name="nl" value="1" checked> Send me the weekly picks.</label>
<button class="btn btn-d" type="submit">Continue with Google</button>
</form>
<p class="muted">Google only. We keep your name and email. Nothing else. <a href="/privacy/">Privacy</a></p>'''
    site.add("/join/", f"{BRAND_EN} — Join", "Join HANSET with Google. Weekly drop-ins and saunas, checked.", f'<div class="page">{jbody}</div>', "about", ("join.js",))
    site.add("/join/welcome/", "You're in", "Welcome to HANSET.", '<div class="page"><h1>You&#x27;re in.</h1><p><a class="btn btn-d" href="/">Back home</a></p></div>', "about", noindex=True)

    guides(site, venues, today)
    site.add("/404.html", "Page not found", "This page doesn’t exist.", f'<div class="page"><h1>Lost? Let’s get you back.</h1><p>This page doesn’t exist. Start again from <a href="/">Home</a> or the <a href="/finder/">{FINDER_LABEL}</a>.</p></div>')

def asset_ver(rel):
    import hashlib
    return hashlib.md5((ROOT / "static" / rel).read_bytes()).hexdigest()[:8]

ILLUSTRATIONS = [
    ("box", "crossfit-box-plan", "Inside a CrossFit box", "Hand-drawn plan of a typical CrossFit box: whiteboard, WOD floor with kettlebells and boxes, rig, open gym with barbell racks, lockers, rowers and bikes, front desk and entrance."),
    ("dropin", "dropin-steps", "How a drop-in works", "Hand-drawn drop-in steps: 1 check class times and price online, 2 book or message the box, 3 say drop-in at the desk, 4 train, 5 put gear back and pay."),
    ("sauna", "sauna-guide", "How to sauna", "Hand-drawn sauna plan and order: lockers, shower, dry or wet sauna, optional cold plunge, rest and water. Skip it if pregnant, unwell, after drinking or with heart issues."),
]

def ics(events, today):
    def esc(s): return s.replace("\r\n", "\n").replace("\r", "\n").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
    def fold(line):
        parts, part, size = [], "", 0
        for char in line:
            width = len(char.encode("utf-8"))
            if size + width > 75:
                parts.append(part)
                part, size = " ", 1
            part += char
            size += width
        return "\r\n".join(parts + [part])
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//HANSET//events//EN", "CALSCALE:GREGORIAN", "X-WR-CALNAME:HANSET Events"]
    for e in [x for x in events if x["end"] >= today]:
        status = f' · {OUTDATED}' if is_outdated(e["last_verified"], today) else ""
        lines += ["BEGIN:VEVENT", f"UID:{e['slug']}@hanset", f"DTSTAMP:{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}",
                  f"DTSTART;VALUE=DATE:{e['start']:%Y%m%d}", f"DTEND;VALUE=DATE:{(e['end'] + dt.timedelta(days=1)):%Y%m%d}",
                  f"SUMMARY:{esc(e['title'])}", f"LOCATION:{esc(e['venue'] + ', ' + e['city'])}", f"URL:{e['url']}",
                  "DESCRIPTION:" + esc("Dates from the official page. Check fees and registration on the official page. "
                                        f'Checked {dot(e["last_verified"])} · {e["source_label"]}{status}'), "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"

def subset_fonts(dist):
    try:
        from fontTools import subset
        from fontTools.ttLib import TTFont
    except ImportError:
        print("WARN: fonttools not installed; skipping font subsetting (site falls back to system fonts). Use .venv/bin/python.")
        return
    corpus = []
    for p in list(dist.rglob("*.html")) + list((dist / "assets/js").glob("*.js")):
        try:
            corpus.append(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            # The mandatory post-generation leak check reports unreadable text
            # safely. Font collection must not print raw decoding exceptions.
            continue
    text = "".join(corpus)
    chars = "".join(sorted(set(text + "".join(chr(c) for c in range(0x20, 0x7F)) + "₩–—·→›×℃™…“”‘’")))
    out = dist / "assets/fonts"; out.mkdir(parents=True, exist_ok=True)
    for w in ("Regular", "Bold"):
        opts = subset.Options(); opts.flavor = "woff2"; opts.layout_features = ["*"]; opts.name_IDs = ["*"]; opts.notdef_outline = True
        f = TTFont(ROOT / f"static/fonts/Pretendard-{w}.otf")
        s = subset.Subsetter(opts); s.populate(text=chars); s.subset(f)
        f.flavor = "woff2"; f.save(out / f"Pretendard-{w}.subset.woff2")
    shutil.copy(ROOT / "static/fonts/Pretendard-OFL-LICENSE.txt", out / "Pretendard-OFL-LICENSE.txt")

# Leak rules are deliberately a vocabulary, not a semantic claim detector. Keep
# diagnostics separate from inspected values: only (relative path, rule) escapes.
LEAK_SUFFIXES = {'.html', '.svg', '.css', '.js', '.json', '.xml', '.ics'}
LEAK_EMAIL = re.compile(r"[\w.!#$%&'*+/=?^`{|}~\-]+@[\w\-]+(?:\.[\w\-]+)+", re.I)
# Korean mobile, regional, internet and service numbers; overseas numbers need
# an explicit +country/00country prefix. No generic long-number heuristic.
_PHONE_SEP = r'[\s.()\-–]*'
LEAK_PHONE = re.compile(
    r'(?<![\w₩$])(?<!\d[.,])(?:'
    r'(?:0(?:1[016789]|2|31|32|33|41|42|43|44|51|52|53|54|55|61|62|63|64|70|80))' + _PHONE_SEP + r'\d{3,4}' + _PHONE_SEP + r'\d{4}'
    r'|1[568]\d{2}' + _PHONE_SEP + r'\d{4}'
    r'|(?:\+|00)\d{1,3}' + _PHONE_SEP + r'(?:\(0\)' + _PHONE_SEP + r')?\d(?:[\s.()\-–]*\d){6,11}'
    r')(?!\w|[.,]\d|\s*(?:원|KRW|USD)\b)', re.I)
LEAK_ADVERTISING = re.compile(
    r'광[\W_]*고|협[\W_]*찬|제[\W_]*휴|후[\W_]*원|스[\W_]*폰[\W_]*서|'
    r'파[\W_]*트[\W_]*너[\W_]*십|미[\W_]*디[\W_]*어[\W_]*킷|노[\W_]*출[\W_]*료|'
    r'sponsor|advertis|partnership|media[\W_]*kit|collaborat', re.I)
LEAK_HEALTH = re.compile(
    r'디[\W_]*톡[\W_]*스|해[\W_]*독|면역(?:력)?[\W_]*(?:증가|증진|강화|향상)|'
    r'살[\W_]*빠(?:진다|짐|지)|체중[\W_]*(?:감량|감소)|지방[\W_]*연소|치유|완치|힐링|'
    r'\bdetox\w*|\bheal(?:s|ing|ed)?\b|\bcur(?:e[sd]?|ing)\b|fat[\W_]*burn\w*|'
    r'weight[\W_]*loss|los(?:e|ing)[\W_]*weight|'
    r'(?:boost\w*|enhanc\w*|improv\w*|increas\w*|strengthen\w*)[\W_]*(?:the[\W_]+)?immun\w*|'
    r'immun\w*[\W_]*(?:boost\w*|enhanc\w*|improv\w*|increas\w*)', re.I)
LEAK_TRACKER = re.compile(r'google-analytics|googletagmanager|facebook\.net|doubleclick|hotjar|clarity\.ms|kakao\.com/js', re.I)
# The legacy numeric owner reference belongs in prose, never SVG coordinates or
# numeric code tokens. All other historical terms are retained on raw/decoded text.
LEAK_LEGACY = re.compile('|'.join(
    '(?i:' + p.replace('(?i)', '') + ')' if '(?i)' in p else '(?:' + p + ')'
    for p in FORBIDDEN if p != r'\b442\b'))


def _js_unescape(value):
    def replace(m):
        part = m.group(1)
        if part.startswith(('u{', 'u', 'x')):
            try:
                return chr(int(part[2:-1] if part.startswith('u{') else part[1:], 16))
            except (ValueError, OverflowError):
                return ''
        return {'n': '\n', 'r': '\r', 't': '\t', 'b': '\b', 'f': '\f', 'v': '\v', '0': '\0',
                '\n': '', '\r\n': ''}.get(part, part)
    return re.sub(r'\\(u\{[0-9a-fA-F]+\}|u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|\r\n|[\s\S])', replace, value)


def _css_unescape(value):
    def replace(m):
        if m.group(1):
            n = int(m.group(1), 16)
            return chr(n) if 0 < n <= 0x10ffff else '\ufffd'
        return '' if m.group(2) in ('\n', '\r', '\f') else m.group(2)
    return re.sub(r'\\(?:([0-9a-fA-F]{1,6})\s?|([\s\S]))', replace, value)


# This lexer preserves strings/comments as tokens; operation names inside them
# cannot turn source-link strings into loads. It does not run any JavaScript.
_JS_TOKEN = re.compile(
    r'(?P<comment>//[^\r\n]*|/\*[\s\S]*?\*/)|'
    r'''(?P<string>"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*'|`(?:\\[\s\S]|[^`\\])*`)|'''
    r'(?P<number>\b(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b)|'
    r'(?P<id>(?:[$\w]|\\u[0-9a-fA-F]{4}|\\u\{[0-9a-fA-F]+\})+)|'
    r'(?P<punct>===|!==|=>|==|!=|\+=|\?\.|[^\s])')


def _js_tokens(text):
    tokens, prose = [], []
    pos = 0
    while (m := _JS_TOKEN.search(text, pos)) is not None:
        pos = m.end()
        kind, value = m.lastgroup, m.group()
        if kind == 'punct' and value == '/' and (not tokens or tokens[-1][1] in {'=', '(', '[', ',', ':', '!', '?', 'return', '=>'}):
            # Skip regexp literals, including escaped slashes and character
            # classes, so names such as fetch in a regexp are inert.
            end, bracket = pos, False
            while end < len(text) and text[end] not in '\r\n':
                c = text[end]
                if c == '\\': end += 2; continue
                if c == '[': bracket = True
                if c == ']': bracket = False
                if c == '/' and not bracket:
                    end += 1
                    while end < len(text) and text[end].isalpha(): end += 1
                    tokens.append(('regex', text[m.start():end])); pos = end; break
                end += 1
            if pos == end: continue
        if kind == 'comment':
            prose.append(value)
        elif kind == 'string':
            decoded = _js_unescape(value[1:-1])
            prose.extend((value[1:-1], decoded))
            interpolation = value[0] == '`' and re.search(r'(?<!\\)(?:\\\\)*\$\{', value[1:-1])
            tokens.append(('dynamic' if interpolation else 'string', decoded))
        else:
            tokens.append((kind, _js_unescape(value) if kind == 'id' else value))
    return tokens, prose


def _js_arguments(tokens, start):
    """Split a call/parenthesized expression without losing nested expressions."""
    args, current, stack = [], [], []
    for i in range(start + 1, len(tokens)):
        v = tokens[i][1]
        if tokens[i][0] == 'punct':
            if v == ')' and not stack:
                return args + [current], i
            if v == ',' and not stack:
                args.append(current); current = []; continue
            if v in ('(', '[', '{'): stack.append(v)
            if v in (')', ']', '}') and stack: stack.pop()
        current.append(tokens[i])
    return args + [current], len(tokens)


def _js_constant(tokens, constants):
    """Only strings, static templates, immutable constants, parentheses and +."""
    i = 0
    def expression():
        nonlocal i
        result = atom()
        while i < len(tokens) and tokens[i] == ('punct', '+'):
            i += 1
            other = atom()
            result = result + other if result is not None and other is not None else None
        return result
    def atom():
        nonlocal i
        if i >= len(tokens): return None
        kind, value = tokens[i]; i += 1
        if kind == 'string': return value
        if kind == 'id': return constants.get(value)
        if (kind, value) == ('punct', '('):
            result = expression()
            if i < len(tokens) and tokens[i] == ('punct', ')'):
                i += 1
                return result
        return None
    result = expression()
    return result if i == len(tokens) else None


class _LeakScanner:
    def __init__(self, path):
        self.path = path
        self.rules = set()
        self.seen = set()

    def words(self, text, prose=True):
        if len(text) < 4096:
            if (text, prose) in self.seen: return
            self.seen.add((text, prose))
        decoded = html.unescape(text) if '&' in text else text
        values = {text, decoded}
        if '%' in decoded: values.add(unquote(decoded))
        for value in values:
            if LEAK_ADVERTISING.search(value): self.rules.add('wording.advertising')
            if LEAK_HEALTH.search(value): self.rules.add('wording.health')
            if LEAK_LEGACY.search(value): self.rules.add('wording.forbidden')
            if LEAK_TRACKER.search(value): self.rules.add('resource.tracker')
            if '@' in value and any(m.group().casefold() != 'hanset@soulbound.co.kr' for m in LEAK_EMAIL.finditer(value)):
                self.rules.add('contact.email')
            if re.search(r'\b(?:tel|sms)\s*:', value, re.I): self.rules.add('contact.phone')
            if prose:
                if LEAK_PHONE.search(value): self.rules.add('contact.phone')
                if re.search(r'\b442\b', value): self.rules.add('wording.forbidden')

    def url(self, value):
        if value is None:
            self.rules.add('resource.unresolved'); return
        # WHATWG URLs accept backslashes and ignore ASCII tabs/newlines. Origin
        # comparison uses parsed components, never string-prefix matching.
        value = re.sub(r'[\x00-\x20\x7f]', '', html.unescape(value)).replace('\\', '/')
        if not value or value.startswith('#') or value.lower().startswith('data:'): return
        # Browsers treat additional leading slashes as an authority, whereas
        # urllib treats three slashes as a local path. Normalize that difference.
        value = re.sub(r'^([a-zA-Z][a-zA-Z0-9+.-]*:)?/{2,}', lambda m: (m.group(1) or '') + '//', value)
        try:
            u = urlparse(urljoin(SITE_URL + '/' + self.path, value))
            port = u.port if u.port is not None else {'https': 443, 'http': 80}.get(u.scheme.lower())
            origin = (u.scheme.lower(), unquote(u.hostname or '').lower(), port)
            site = urlparse(SITE_URL)
            expected = (site.scheme, site.hostname, site.port if site.port is not None else 443)
            if u.username is not None or u.password is not None or origin != expected:
                self.rules.add('resource.external')
        except (ValueError, UnicodeError):
            self.rules.add('resource.unresolved')

    def srcset(self, value):
        # A data URI may itself contain commas. Whitespace separates its optional
        # descriptor; other candidates begin after the candidate-separator comma.
        for m in re.finditer(r'(?:^|,\s*)\s*(data:[^\s]+|[^\s,]+)', value):
            self.url(m.group(1))

    def css(self, text):
        self.words(text, prose=False)
        decoded = _css_unescape(text)
        self.words(decoded, prose=False)
        for m in re.finditer(r'/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', decoded):
            self.words(m.group())
        # Comments can split @import and url tokens. Strip them only for the
        # load scanner; their contents were still checked above.
        code = re.sub(r'/\*[\s\S]*?\*/', '', decoded)
        for m in re.finditer(r'''url\(\s*(?:"([^"]*)"|'([^']*)'|([^)]*))\s*\)|@import\s+(?:"([^"]*)"|'([^']*)')|"[^"]*"|'[^']*' ''', code, re.I | re.X):
            destination = next((v for v in m.groups() if v is not None), None)
            if destination is not None: self.url(destination.strip())

    def javascript(self, text):
        self.words(text, prose=False)
        tokens, prose = _js_tokens(text)
        for value in prose: self.words(value)
        for i, token in enumerate(tokens):
            if token[0] != 'string': continue
            end = i+1
            while end+1 < len(tokens) and tokens[end] == ('punct', '+') and tokens[end+1][0] == 'string':
                end += 2
            if end > i+1:
                self.words(''.join(v for k, v in tokens[i:end] if k == 'string'))
        # Normalize static bracket members after decoding, so escaped property
        # names receive exactly the same checks as dotted property names.
        i = 0
        while i < len(tokens):
            if tokens[i] == ('punct', '['):
                end = i+1
                while end < len(tokens) and tokens[end] != ('punct', ']'): end += 1
                member = _js_constant(tokens[i+1:end], {})
                if member and re.fullmatch(r'[A-Za-z_$][\w$]*', member):
                    tokens[i:end+1] = [('punct', '.'), ('id', member)]
            i += 1
        constants = {}
        parameters = set()
        for i, token in enumerate(tokens):
            if token == ('id', 'function'):
                start = i+1
                while start < len(tokens) and tokens[start] != ('punct', '('): start += 1
                if start < len(tokens):
                    args, _ = _js_arguments(tokens, start)
                    parameters.update(v for arg in args for k, v in arg if k == 'id')
            elif token == ('punct', '=>') and i:
                if tokens[i-1][0] == 'id': parameters.add(tokens[i-1][1])
                elif tokens[i-1] == ('punct', ')'):
                    depth, start = 1, i-2
                    while start >= 0 and depth:
                        if tokens[start] == ('punct', ')'): depth += 1
                        if tokens[start] == ('punct', '('): depth -= 1
                        start -= 1
                    parameters.update(v for k, v in tokens[start+2:i-1] if k == 'id')
        for i, t in enumerate(tokens[:-3]):
            if t == ('id', 'const') and tokens[i+1][0] == 'id' and tokens[i+2] == ('punct', '='):
                name = tokens[i+1][1]
                end = i+3
                while end < len(tokens) and tokens[end] not in (('punct', ';'), ('punct', ',')): end += 1
                # Repeated declarations/shadowing are deliberately unresolved.
                declarations = sum(tokens[j][1] in {'const', 'let', 'var'} and tokens[j+1] == ('id', name) for j in range(len(tokens)-1))
                if declarations == 1 and name not in parameters:
                    constants[name] = _js_constant(tokens[i+3:end], constants)
                    if constants[name] is not None: self.words(constants[name])
        def value(arg): return _js_constant(arg, constants)
        def load(arg, kind='url'):
            v = value(arg)
            if kind == 'css' and v is not None: self.css(v)
            elif kind == 'html' and v is not None: self.markup(v)
            elif kind == 'srcset' and v is not None: self.srcset(v)
            else: self.url(v)
        values = [v for _, v in tokens]
        request_calls = {'fetch', 'sendBeacon', 'WebSocket', 'EventSource', 'Worker', 'SharedWorker', 'Request', 'import', 'importScripts'}
        resource_attrs = {'src', 'href', 'srcset', 'poster', 'data', 'action', 'formaction', 'codebase', 'background'}
        for i, (kind, name) in enumerate(tokens):
            if kind != 'id': continue
            # ES module import/export declarations. Dynamic import is a call.
            if name in ('import', 'export') and i+1 < len(tokens) and values[i+1] != '(':
                if tokens[i+1][0] == 'string': self.url(values[i+1])
                else:
                    for j in range(i+1, len(tokens)-1):
                        if values[j] == ';': break
                        if tokens[j] == ('id', 'from'):
                            self.url(values[j+1] if tokens[j+1][0] == 'string' else None); break
            # Property writes, including automatic navigation. Bracket notation
            # with a static property name was normalized above too.
            assignment = i+1
            if (i > 0 and values[i-1] in ('.', '?.')) or name == 'location':
                if assignment < len(tokens) and values[assignment] in ('=', '+=') and name in resource_attrs | {'location', 'cssText', 'innerHTML', 'outerHTML'}:
                    end = assignment+1
                    depth = 0
                    while end < len(tokens):
                        v = values[end]
                        if tokens[end][0] == 'punct':
                            if depth == 0 and v in (';', ',', ')', '}'): break
                            if v in ('(', '[', '{'): depth += 1
                            if v in (')', ']', '}'): depth -= 1
                        end += 1
                    load(tokens[assignment+1:end] if values[assignment] == '=' else [],
                         'css' if name == 'cssText' else 'html' if name in ('innerHTML', 'outerHTML') else 'srcset' if name == 'srcset' else 'url')
            call = i+1
            if call < len(tokens) and tokens[call] == ('punct', '?.'): call += 1
            if call >= len(tokens) or tokens[call] != ('punct', '('): continue
            args, _ = _js_arguments(tokens, call)
            prefix = values[max(0, i-4):i]
            if name in request_calls:
                for arg in (args if name == 'importScripts' else args[:1]): load(arg)
            elif name == 'open':
                navigation = i == 0 or values[i-1] not in ('.', '?.') or 'window' in prefix or 'globalThis' in prefix
                load(args[0] if navigation else args[1] if len(args) > 1 else [])
            elif name == 'register' and 'serviceWorker' in prefix: load(args[0])
            elif name in ('assign', 'replace') and 'location' in prefix: load(args[0])
            elif name in ('setAttribute', 'setAttributeNS'):
                offset = 1 if name == 'setAttributeNS' else 0
                attr = value(args[offset]) if len(args) > offset else None
                if attr and attr.lower() in resource_attrs | {'style'}:
                    load(args[offset+1] if len(args) > offset+1 else [], 'css' if attr == 'style' else 'srcset' if attr == 'srcset' else 'url')
                elif attr is None: self.rules.add('resource.unresolved')
            elif name in ('insertRule', 'replaceSync'): load(args[0], 'css')
            elif name in ('write', 'writeln') and 'document' in prefix:
                for arg in args: load(arg, 'html')
            elif name == 'insertAdjacentHTML': load(args[1] if len(args) > 1 else [], 'html')

    def json(self, text):
        self.words(text, prose=False)
        try:
            obj = json.loads(text)
        except (ValueError, RecursionError):
            self.rules.add('validation.parse'); return
        def walk(value):
            if isinstance(value, str): self.words(value)
            elif isinstance(value, dict):
                for key, item in value.items(): self.words(key); walk(item)
            elif isinstance(value, list):
                for item in value: walk(item)
        walk(obj)

    def markup(self, text):
        self.words(text, prose=False)
        entities = dict(re.findall(r'<!ENTITY\s+([\w-]+)\s+["\']([^"\']*)["\']\s*>', text, re.I))
        # Expand only bounded, internal string entities. No XML parser, DTD
        # retrieval or network entity resolution is involved.
        for _ in range(8):
            expanded = re.sub(r'&([\w-]+);', lambda m: entities.get(m.group(1), m.group()), text)
            if expanded == text: break
            if len(expanded) > max(1048576, len(text) * 4):
                self.rules.add('validation.parse'); return
            text = expanded
        else:
            self.rules.add('validation.parse'); return
        parser = _LeakMarkup(self)
        try:
            parser.feed(text); parser.close(); parser.flush()
        except (ValueError, AssertionError, RecursionError):
            self.rules.add('validation.parse')
        # HTMLParser never resolves DTDs/entities. Only inspect declarations and
        # stylesheet processing instructions; namespace/source URLs are inert.
        for m in re.finditer(r'<![^>]*\b(?:SYSTEM|PUBLIC)\s+(?:"[^"]*"\s+)?["\']([^"\']+)["\']', text, re.I):
            self.url(m.group(1))
        for m in re.finditer(r'<\?xml-stylesheet\b[^?]*\?>', text, re.I):
            href = re.search(r'''\bhref\s*=\s*['"]([^'"]+)['"]''', m.group(), re.I)
            self.url(href.group(1) if href else None)
        for m in re.finditer(r'<!ENTITY\s+\S+\s+["\']([^"\']*)["\']\s*>', text, re.I):
            self.words(m.group(1))

    def calendar(self, text):
        text = re.sub(r'\r?\n[ \t]', '', text)
        text = re.sub(r'\\([nN,;\\])', lambda m: '\n' if m.group(1).lower() == 'n' else m.group(1), text)
        self.words(text)
        for line in text.splitlines():
            if re.match(r'ATTACH(?:;|:)', line, re.I):
                head, _, value = line.partition(':')
                if 'VALUE=BINARY' not in head.upper(): self.url(value)


class _LeakMarkup(HTMLParser):
    # SVG geometry and other purely numeric presentation attributes are code,
    # while text/title/desc, metadata, comments and string attributes are prose.
    GEOMETRY = {'d', 'points', 'viewbox', 'transform', 'gradienttransform', 'x', 'y', 'x1', 'x2', 'y1', 'y2',
                'cx', 'cy', 'r', 'rx', 'ry', 'width', 'height', 'offset', 'opacity', 'stroke-width', 'pathlength'}
    def __init__(self, scanner):
        super().__init__(convert_charrefs=True)
        self.scanner = scanner
        self.block = None
        self.chunks = []

    def flush(self):
        if self.block:
            text = ''.join(self.chunks)
            if self.block == 'style': self.scanner.css(text)
            elif self.block == 'json': self.scanner.json(text)
            elif self.block == 'script': self.scanner.javascript(text)
            else: self.scanner.words(text)
            self.block = None; self.chunks = []

    def handle_starttag(self, tag, attrs):
        tag = tag.rsplit(':', 1)[-1]
        s = self.scanner
        attrs = dict(attrs)
        for key, value in attrs.items():
            if value is None: continue
            s.words(value, prose=key not in self.GEOMETRY and key not in {'style'} and not key.startswith('on'))
            if key == 'style': s.css(value)
            if key.startswith('on'): s.javascript(value)
            if key in {'src', 'poster', 'background', 'codebase', 'manifest'} or (key == 'data' and tag == 'object'):
                s.url(value)
            if key in {'srcset', 'imagesrcset'}: s.srcset(value)
            if key == 'ping':
                for url in value.split(): s.url(url)
            if key == 'srcdoc': s.markup(value)
        if tag in {'image', 'use', 'feimage', 'base', 'script'}:
            for key in ('href', 'xlink:href'):
                if key in attrs: s.url(attrs[key])
        if tag == 'link':
            rel = set((attrs.get('rel') or '').lower().split())
            if rel - {'canonical', 'alternate', 'author', 'help', 'license', 'next', 'prev'}:
                if 'href' in attrs: s.url(attrs['href'])
        if tag == 'meta' and (attrs.get('http-equiv') or '').strip().lower() == 'refresh':
            m = re.search(r'url\s*=\s*(.*)', attrs.get('content') or '', re.I)
            if m: s.url(m.group(1).strip(' \t\'"'))
        # SMIL may change resource destinations without scripting.
        if tag in {'animate', 'set'} and (attrs.get('attributename') or '').lower() in {'href', 'xlink:href', 'src'}:
            for key in ('to', 'from', 'values'):
                for value in (attrs.get(key) or '').split(';'):
                    if value: s.url(value)
        if tag in {'script', 'style'}:
            self.flush()
            script_type = (attrs.get('type') or '').lower()
            self.block = 'style' if tag == 'style' else 'json' if script_type in {'application/json', 'application/ld+json', 'importmap'} else 'script'

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.rsplit(':', 1)[-1]
        if tag in {'script', 'style'}: self.flush()

    def handle_data(self, text):
        if self.block: self.chunks.append(text)
        else: self.scanner.words(text)

    def handle_comment(self, text):
        self.scanner.words(text)

    def handle_pi(self, text):
        self.scanner.words(text)

    def unknown_decl(self, text):
        self.scanner.words(text)


def leak_check(dist):
    """Inspect emitted text without network access; return safe, stable findings."""
    bad = set()
    for p in sorted(dist.rglob('*')):
        if not p.is_file() or p.suffix.lower() not in LEAK_SUFFIXES: continue
        rel = p.relative_to(dist).as_posix()
        scanner = _LeakScanner(rel)
        try:
            text = p.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            bad.add((rel, 'validation.read')); continue
        suffix = p.suffix.lower()
        if suffix in {'.html', '.svg', '.xml'}: scanner.markup(text)
        elif suffix == '.css': scanner.css(text)
        elif suffix == '.js': scanner.javascript(text)
        elif suffix == '.json': scanner.json(text)
        else: scanner.calendar(text)
        bad.update((rel, rule) for rule in scanner.rules)
    return sorted(bad)

ARCHIVE_DATE = "2026.10.18"
# (file, venue slug, title, short title, alt text, source label, source URL)
ARCHIVE = [
    ("archive-sisu-house", "sisu-house", "Sisu House, floor by floor", "Sisu House", "Hand drawing stacking the floors of Sisu House (1F Welcoming Zone, 1.5F Touch Zone, 2-3F Private Zone) and its ritual sequence.", "sisu.house", "https://sisu.house/about"),
    ("archive-spa-haesurang", "spa-haesurang-busan", "Spa Haesurang, floor by floor", "Spa Haesurang", "Hand drawing stacking the floors of Spa Haesurang (2F lobby, 3–4F powder rooms, 4–5F locker rooms, 5F jjimjil room, 7F open-air bath). No official info for 6F.", "spahaesurang.co.kr", "https://spahaesurang.co.kr/amenities"),
    ("archive-heosimcheong", "heosimcheong-busan", "Hurshimchung, floor by floor", "Hurshimchung (Hotel Nongshim)", "Hand drawing stacking the floors of Hotel Nongshim Hurshimchung (B1·B2 parking, 1F restaurant, 2F banquet, 3F jjimjil room, 4F lockers and front desk, 5F hot-spring baths).", "hotelnongshim.com", "https://www.hotelnongshim.com/kr/?pCode=Hurshimchung"),
    ("archive-termeden", "termeden-icheon", "Termeden, zone by zone", "Termeden", "Hand drawing of Termeden’s zones (indoor pool & spa, hot-spring bathhouse, jjimjil spa, outdoor pool & spa) on one sheet.", "termeden.com", "https://termeden.com/poolspa_explain.html"),
    ("archive-orakai-cheonggyesan", "orakai-cheonggyesan", "Orakai Cheonggyesan, floor by floor", "Orakai Cheonggyesan", "Hand drawing stacking the B1 sauna (dry and wet saunas, several pools) and the B2 EV charging area of Orakai Cheonggyesan Hotel. The sauna’s interior layout is not drawn.", "cgs.orakaihotels.com", "https://cgs.orakaihotels.com/kr/about/facilities_sauna.asp"),
    ("archive-crossfit-super-sapiens", "crossfit-super-sapiens", "CrossFit Super Sapiens, floor by floor", "CrossFit Super Sapiens", "Hand drawing stacking the floors of CrossFit Super Sapiens (1F main training floor, 2F showers, lockers and accessory area, 3F tanning and staff space).", "crossfitsupersapiens.com", "https://crossfitsupersapiens.com/our-facility"),
]
ARCHIVE_BY_VENUE = {a[1]: a for a in ARCHIVE}
# 조감도 손그림 (위에서 내려다본 평면). illustrations/make_aerial.py
AERIAL = [
    ('aerial-termeden', 'termeden-icheon', "Hand-drawn bird's-eye plan of Termeden in Icheon: outdoor infinity pool, forest bath, mountain pool and terme tubs; indoor pool and spa, hot-spring bathhouse with cold bath and saunas, and the jjimjil spa; day pass ₩49,000 (adult, November)."),
    ('aerial-spa-haesurang', 'spa-haesurang-busan', "Hand-drawn bird's-eye plan of Spa Haesurang's sauna floor in Busan: dry and wet saunas, warm, hot and cold pools, event bath, rows of showers (about 80) and powder room; sauna ₩15,000 on weekdays."),
    ('aerial-heosimcheong', 'heosimcheong-busan', "Hand-drawn bird's-eye plan of the 5th-floor hot-spring baths at Hurshimchung, Busan: longevity, event, celadon, cave, cold, half-body, salt and open-air baths, mini pool, steam and dry saunas, showers and scrub corner; ₩15,000 on weekdays."),
    ('aerial-orakai-cheonggyesan', 'orakai-cheonggyesan', "Hand-drawn bird's-eye plan of the basement sauna at Orakai Cheonggyesan Hotel, Seoul: dry and wet saunas, several pools, showers; adult ₩24,000."),
    ('aerial-sisu-house', 'sisu-house', "Hand-drawn bird's-eye plan of Sisu House in Seoul: a one-person private room with Finnish sauna, warm bath, 14°C cold shower, ice fountain and changing room, plus the touch-care room and check-in; from ₩79,000."),
    ('aerial-crossfit-super-sapiens', 'crossfit-super-sapiens', "Hand-drawn bird's-eye plan of CrossFit Super Sapiens in Seoul: 1st-floor training floor with rig and barbells, 2nd-floor accessory area, showers and lockers, 3rd-floor tanning and staff room; drop-in ₩30,000."),
]
AERIAL_BY_VENUE = {a[1]: a for a in AERIAL}


# 2026-10-29 owner order (준태님): the OLD floor-stacking venue sketches (archive-*) are NOT aerial views.
# Never call them 조감도 / aerial / bird's-eye / from above / isometric.
# 2026-10-30: the NEW true top-down drawings (/assets/img/aerial/, illustrations/*aerial*) ARE 조감도 and may use those words,
# but only inside <!--aerial-->..<!--/aerial--> blocks in HTML and in their own asset/source files.
VIEW_WORD_BAN = r"(?i)조감|aerial|bird[’'`]?s?[- ]?eye|from above|isometric|아이소메트릭|내려다[본보]"
AERIAL_BLOCK = re.compile(r"(?s)<!--aerial-->.*?<!--/aerial-->")
def _aerial_allowed(p):
    s = str(p).replace("\\", "/")
    return "/assets/img/aerial/" in s or "/img/aerial/" in s or Path(s).name.startswith(("aerial-", "make_aerial")) or Path(s).name == "make_guide.py"  # source import only; guide SVG output is still scanned
def view_word_scan(path, text):
    """Return the banned word found, or None. Aerial assets pass; aerial HTML blocks are stripped first."""
    if _aerial_allowed(path): return None
    m = re.search(VIEW_WORD_BAN, AERIAL_BLOCK.sub("", text))
    return m.group(0) if m else None
def view_word_check(dist):
    bad = []
    ill = Path(__file__).resolve().parent.parent / "illustrations"
    srcs = [p for p in dist.rglob("*") if p.is_file() and p.suffix in (".html", ".xml", ".txt", ".json", ".svg", ".js", ".ics")]
    srcs += list(ill.glob("*.py")) + list(ill.glob("svg/*.svg"))
    for p in srcs:
        w = view_word_scan(p, p.read_text(errors="ignore"))
        if w: bad.append((str(p), w))
    return bad

# 2026-10-30 owner order (준태님): no "conceptual / not to scale / may differ / source" captions on drawings.
DISCLAIMER_BAN = re.compile(r"(?i)conceptual|not to scale|may differ|not the real layout|hand-drawn from the official|based on official|개념도|제 비율|실제와 다를|출처")
def disclaimer_check(dist):
    bad = []
    ill = Path(__file__).resolve().parent.parent / "illustrations"
    srcs = list(ill.glob("svg/*.svg")) + [p for p in dist.rglob("*.svg")]
    for p in srcs:
        m = DISCLAIMER_BAN.search(p.read_text(errors="ignore"))
        if m: bad.append((str(p), m.group(0)))
    for p in dist.rglob("*.html"):
        t = p.read_text(errors="ignore")
        for fig in re.finditer(r"<(figure|article|a|section)\b[^>]*>(?:(?!</\1>).)*?(?:img/(?:aerial|archive|guide)/)(?:(?!</\1>).)*?</\1>", t, re.S):
            m = DISCLAIMER_BAN.search(fig.group(0))
            if m: bad.append((str(p), m.group(0))); break
    return bad

# 2026-10-30 owner order: every drawing is English. Any Hangul in a drawing SVG fails the build.
HANGUL = re.compile(r"[\u1100-\u11FF\u3130-\u318F\uAC00-\uD7A3]")
def drawing_hangul_scan(text):
    return sorted(set(HANGUL.findall(text)))
def drawing_hangul_check(dist):
    bad = []
    ill = Path(__file__).resolve().parent.parent / "illustrations"
    # Scope: venue drawings (aerial-*, archive-*) and the guide drawings (img/guide).
    guide = ("sauna-guide", "crossfit-box-plan", "dropin-steps")
    srcs = list(ill.glob("svg/aerial-*.svg")) + list(ill.glob("svg/archive-*.svg")) + [ill / f"svg/{g}.svg" for g in guide if (ill / f"svg/{g}.svg").exists()]
    srcs += list((Path(__file__).resolve().parent / "static/img/guide").glob("*.svg"))
    srcs += [p for d in ("assets/img/aerial", "assets/img/archive", "assets/img/guide") for p in (dist / d).glob("*.svg")]
    for p in srcs:
        h = drawing_hangul_scan(p.read_text(errors="ignore"))
        if h: bad.append((str(p), "".join(h[:10])))
    return bad

# 2026-10-29 owner order (준태님): people hate reading. Key pages must not carry long paragraphs.
LONG_P_LIMIT = 140
LONG_P_KEY = ("index.html", "finder/index.html", "archive/index.html", "events/index.html", "guides/index.html")
def long_text_check(dist):
    bad = []
    for p in dist.rglob("index.html"):
        rel = str(p.relative_to(dist))
        if not (rel in LONG_P_KEY or rel.startswith("finder/")): continue
        t = re.sub(r"(?s)<details.*?</details>|<footer.*?</footer>", "", p.read_text(encoding="utf-8"))
        for para in re.findall(r"(?s)<p(?:\s[^>]*)?>(.*?)</p>", t):
            txt = html.unescape(re.sub(r"<[^>]+>", "", para)).strip()
            if len(txt) > LONG_P_LIMIT: bad.append((rel, len(txt), txt[:60]))
    return bad

def main():
    # Intercept the old option and its argparse abbreviations before any value can be echoed.
    if any(arg.split("=", 1)[0] in ("--ads", "--ad", "--a") for arg in sys.argv[1:]):
        sys.exit("ERROR: --ads has been removed. Advertising is disabled.")
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", action="store_true")
    ap.add_argument("--today")
    ap.add_argument("--out", default=str(DIST))
    ap.add_argument("--verified-examples", action="store_true", help="preview only: render fake data from data/verified/examples")
    a = ap.parse_args()
    today = ymd(a.today) if a.today else dt.datetime.now(ZoneInfo("Asia/Seoul")).date()
    dist = Path(a.out)
    venues, events, skipped, ev_skipped = load(today)
    if a.verified_examples and a.release: sys.exit("ERROR: --verified-examples is preview-only; never with --release")
    if a.verified_examples:
        vevents = VF.load_events(VERIFIED / "examples/events.example.json", today, allow_example=True)
        creators = VF.load_creators(VERIFIED / "examples/creators.example.json", allow_example=True)
    else:
        vevents = VF.load_events(VERIFIED / "events.json", today); creators = VF.load_creators(VERIFIED / "creators.json")
    site = Site(today, a.release); site.creators = bool(creators)
    build(site, venues, events, today, vevents, creators)
    if dist.exists(): shutil.rmtree(dist)
    dist.mkdir(parents=True)
    for path, h in site.pages.items():
        p = dist / path.lstrip("/") if path.endswith(".html") else dist / path.lstrip("/") / "index.html"
        p.parent.mkdir(parents=True, exist_ok=True); p.write_text(h, encoding="utf-8")
    (dist / "assets").mkdir(); shutil.copytree(ROOT / "static/css", dist / "assets/css"); shutil.copytree(ROOT / "static/js", dist / "assets/js"); shutil.copytree(ROOT / "static/img", dist / "assets/img")
    for family in ("Excalifont", "Gaegu"):
        shutil.copytree(ROOT / "static/fonts" / family, dist / "assets/fonts" / family)
    for v in venues:
        image = venue_image_path(v)
        if image:
            destination = dist / "assets/venue-sketch" / image.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(image, destination)
    shutil.copy(ROOT / "static/favicon.svg", dist / "favicon.svg")
    for _f in (ROOT / "static/root").glob("*"):  # 루트 정적 파일(구글 서치콘솔 소유확인 등; 지우지 말 것)
        if _f.is_file(): shutil.copy(_f, dist / _f.name)
    (dist / "events.ics").write_text(ics(events, today), encoding="utf-8")
    urls = [p for p in site.pages if p != "/404.html" and p not in site.noindex]
    if a.release:
        (dist / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n")
        (dist / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(f"<url><loc>{SITE_URL}{u}</loc><lastmod>{today}</lastmod></url>" for u in urls) + "</urlset>\n")
    else:
        (dist / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    (dist / "_headers").write_text("/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: strict-origin-when-cross-origin\n  X-Frame-Options: DENY\n  Permissions-Policy: camera=(), microphone=(), geolocation=()\n"
        "  Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; font-src 'self'; connect-src 'none'; base-uri 'none'; form-action 'self' https://accounts.google.com; frame-ancestors 'none'\n"
        "/assets/*\n  Cache-Control: public, max-age=86400\n/*.html\n  Cache-Control: public, max-age=0, must-revalidate\n")
    subset_fonts(dist)
    bad = leak_check(dist)
    if bad:
        for path, rule in bad: print(f"LEAK-CHECK FAIL: {path} [{rule}]")
        sys.exit(2)
    lt = long_text_check(dist)
    for b in lt: print("LONG-TEXT WARN:", b)
    if lt: sys.exit(5)
    print(f"long-text check OK (no paragraph over {LONG_P_LIMIT} chars on key pages)")
    dh = drawing_hangul_check(dist)
    for b in dh: print("DRAWING-HANGUL CHECK FAIL:", b)
    if dh: sys.exit(6)
    print("drawing Hangul check OK (all drawing SVGs are English)")
    dc = disclaimer_check(dist)
    for b in dc: print("DRAWING-DISCLAIMER CHECK FAIL:", b)
    if dc: sys.exit(8)
    print("drawing disclaimer check OK (no conceptual / not-to-scale / source captions)")
    vw = view_word_check(dist)
    for b in vw: print("VIEW-WORD CHECK FAIL:", b)
    if vw: sys.exit(3)
    print("view-word check OK (old sketches: no 조감도 / aerial / bird's-eye / from above / isometric; aerial drawings allowlisted)")
    total = sum(p.stat().st_size for p in dist.rglob("*") if p.is_file())
    print(f"built {len(site.pages)} pages, {len(venues)} venues ({sum(v['kind_key']=='box' for v in venues)} boxes, {sum(v['kind_key']=='sauna' for v in venues)} saunas), {len(events)} events, {len(vevents)} verified-feed events, {len(creators)} creators -> {dist}  ({total/1024:.0f} KB total)")
    print(f"not published (no verified/partial content): {len(skipped)} venue rows, {len(ev_skipped)} event rows")
    for k, n, c in skipped[:0]: print("  ", k, n, c)
    print("leak check OK (contacts, wording, resources, legacy terms and trackers)")

if __name__ == "__main__":
    main()
