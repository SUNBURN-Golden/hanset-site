#!/usr/bin/env python3
"""HANSET / 한셋 static site builder.

Reads ./boxes.csv ./saunas.csv ./events.csv (single source of truth for numbers, dates, URLs) and
content/venues.json + content/events.json (Korean copy for entries cleared to publish) and writes ./dist.
Only stdlib required; fonttools+brotli (in .venv) are used to subset Pretendard into small woff2 files.
Nothing is deployed, nothing is sent. No trackers, no external requests.

  .venv/bin/python build_site.py                 # preview build (noindex)
  .venv/bin/python build_site.py --release       # indexable + canonical/sitemap for hanset.soulbound.co.kr
  .venv/bin/python build_site.py --ads path.json # test 광고 labels from an alternative ads file
"""
import argparse, csv, datetime as dt, html, json, re, shutil, sys
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo
import verified as VF

ROOT = Path(__file__).resolve().parent
DATA = ROOT.parent
DIST = ROOT / "dist"
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
def wd(d): return WD[d.weekday()]
def span(a, b, ref_year):
    def f(d):
        return (f"{d.month}/{d.day} ({wd(d)})" if d.year == ref_year else f"{d.year}.{d.month}.{d.day} ({wd(d)})")
    return f(a) if a == b else f"{f(a)}–{f(b)}"

FOREIGNER = json.loads((ROOT / "content/foreigner.json").read_text(encoding="utf-8"))
CLOSED = {k: c for k, c in json.loads((ROOT / "content/closed_days.json").read_text(encoding="utf-8")).items() if not k.startswith("_")}
CLOSED_ALL = CLOSED
def active_closed(today):
    return {k: c for k, c in CLOSED_ALL.items() if not c.get("until") or ymd(c["until"]) >= today}
CLOSED = active_closed(dt.datetime.now(ZoneInfo("Asia/Seoul")).date())  # expired date-bound closures hide at build time
CLOSED_CHECK = "Check before you go"
def closed_row(v):
    c = CLOSED.get(v["slug"])
    if not c: return f'<tr><th>Closed on</th><td class="unk">{CLOSED_CHECK}</td></tr>'
    return (f'<tr><th>Closed on</th><td>{E(c["closed"])}<br><small class="fact-source">{E(c["source"])} · checked {dot(ymd(c["checked"]))} · '
            f'<a href="{E(c["url"])}" rel="noopener noreferrer">{E(host(c["url"]))}</a></small></td></tr>')

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
            links.append(f'<a href="{E(c["url"])}" rel="noopener noreferrer">{E(v["title"])}</a>{note}')
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
def fchk(v):
    d = FOREIGNER.get(v["slug"], {})
    keys = SAUNA_PKG_KEYS if (v["kind_key"] == "sauna" and "public_access" in d) else FCHK_KEYS
    rows = "".join(
        f'<tr><th>{lab}</th>' + (f'<td>{E(d[k])}</td>' if _is_confirmed(d.get(k, "")) else '<td class="unk">Not confirmed</td>') + '</tr>'
        for k, lab in keys
    )
    return f'<h2>Foreigner &amp; visitor access checklist</h2><table class="facts"><tbody>{rows}</tbody></table><p class="muted">Official source · {dot(v["last_verified"])}</p>'

GUIDES = [  # (slug, title, description); page bodies are built in guides()
    ("tattoo-friendly-saunas-korea", "Tattoos and saunas in Korea", "What official pages of Korean hotel saunas and spas say about tattoos. Verified, dated, sourced."),
    ("hotel-saunas-korea-non-guests", "Hotel saunas in Korea: can non-guests get in?", "Which Korean hotel saunas are open to visitors without a room, per their official pages."),
    ("crossfit-hyrox-drop-in-korea", "CrossFit & HYROX drop-in in Korea", "CrossFit and HYROX boxes in Korea that list drop-ins or English support on their official pages."),
    ("closed-days-korea", "Closed days: saunas and gyms in Korea", "Confirmed closed days for Korean saunas, spas and CrossFit boxes. Each with its source and check date.")]

def guides(site, venues):
    sau = [v for v in venues if v["kind_key"] == "sauna"]; box = [v for v in venues if v["kind_key"] == "box"]
    def tbl(vs, key, head):
        confirmed = [v for v in vs if _is_confirmed(FOREIGNER.get(v["slug"], {}).get(key, ""))]
        excluded_cnt = len(vs) - len(confirmed)
        r = "".join(f'<tr><th><a href="/finder/{v["slug"]}/">{E(v["title"])}</a><br><span class="muted">{E(v["city_ko"])}</span></th><td>{E(FOREIGNER.get(v["slug"],{}).get(key))}</td><td>{dot(v["last_verified"])} · <a href="{E(v["source"])}" rel="noopener noreferrer">{E(host(v["source"]))}</a></td></tr>' for v in confirmed)
        excl_note = f'<p class="muted">{excluded_cnt} open venue(s) where "{head}" is still Not confirmed on the official page are excluded from this table. See each venue page in the <a href="/finder/">Finder</a> for full details.</p>' if excluded_cnt else ""
        return f'<table class="facts"><thead><tr><th>Venue</th><th>{head}</th><th>Checked · Source</th></tr></thead><tbody>{r}</tbody></table>{excl_note}'
    note = '<p class="muted">Official pages only. Every line links to its source and check date. Venues where a condition is Not confirmed, pre-opening, or temporarily closed are excluded from recommendations below.</p>'
    bodies = {"tattoo-friendly-saunas-korea": "<h1>Tattoos? Read this first.</h1><p class=\"lead\">Some Korean saunas limit entry with tattoos. Here’s exactly what each official page says. Nothing more.</p>" + note + tbl(sau, "tattoo", "Tattoo rule"),
              "hotel-saunas-korea-non-guests": "<h1>No room? Some doors still open.</h1><p class=\"lead\">Hotel saunas in Seoul, Busan and Jeju. Who gets in, straight from the official pages.</p>" + note + tbl(sau, "dropin", "Open to visitors?") + f'<p class="safety">{SAFETY}</p>',
              "crossfit-hyrox-drop-in-korea": "<h1>Traveling? Keep training.</h1><p class=\"lead\">CrossFit and HYROX boxes in Korea. Drop-in and English support, from official pages only.</p>" + note + tbl(box, "dropin", "Drop-in") + "<h2>English support</h2>" + tbl(box, "english", "English")}
    by_slug = {v["slug"]: v for v in venues}
    crow = "".join(f'<tr><th><a href="/finder/{k}/">{E(by_slug[k]["title"])}</a><br><span class="muted">{E(by_slug[k]["city_ko"])}</span></th><td>{E(c["closed"])}</td>'
                   f'<td>{dot(ymd(c["checked"]))} · <a href="{E(c["url"])}" rel="noopener noreferrer">{E(c["source"])}</a></td></tr>'
                   for k, c in sorted(CLOSED.items(), key=lambda kc: (by_slug[kc[0]]["city_key"] != "seoul", by_slug[kc[0]]["city_key"], by_slug[kc[0]]["title"])) if k in by_slug)
    bodies["closed-days-korea"] = ('<h1>Closed today? Check first.</h1><p class="lead">Some Korean saunas and gyms close one day a month. Others close every week. Here are the ones we’ve confirmed.</p>'
        + closed_month_block(site.today, by_slug)
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
def load(today, ads_path):
    content = json.loads((ROOT / "content/venues.json").read_text(encoding="utf-8"))
    ev_content = json.loads((ROOT / "content/events.json").read_text(encoding="utf-8"))
    en = json.loads((ROOT / "content/venues_en.json").read_text(encoding="utf-8"))
    ev_en = json.loads((ROOT / "content/events_en.json").read_text(encoding="utf-8"))
    ads = json.loads(Path(ads_path).read_text(encoding="utf-8")) if ads_path else json.loads((ROOT / "content/ads.json").read_text(encoding="utf-8"))
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
        facts = [[k, fill(v)] for k, v in x["facts"]]
        facts_ko = [[k, v] for k, v in c["facts"]]
        lv = ymd(r["last_verified"])
        a = ads.get(c["slug"], {})
        v = dict(c=c, name=name, slug=c["slug"], kind_key=kind_key, kind=KIND_EN[c["kind"]], status=status, title=x["title"], title_ko=c["title"],
                 city=r["city"], city_ko=CITY_EN.get(r["city"], r["city"]), city_key=CITY_KEY.get(r["city"], r["city"].lower()),
                 location=x["location"], location_ko=c["location"], area=x.get("area", ""), summary=x["summary"], facts=facts, facts_ko=facts_ko, tips=x.get("tips", []), unverified=[fill(u) for u in x.get("unverified", [])],
                 tags=x.get("tags", []), price=price, price_label=x.get("price_label", "Price"),
                 sunday=sunday, last_verified=lv, stale=(today - lv).days > STALE_DAYS, source=src, extra_sources=extra,
                 website=site, booking=book, address=addr, hyrox=bool(c.get("hyrox")), pick=bool(c.get("pick")),
                 price_caveat=x.get("price_caveat", ""), sub=("box" if kind_key == "box" else (c.get("sub") or ("hotel" if "호텔" in c["kind"] else "boutique"))), ad=bool(a.get("ad", c.get("ad", False))), ad_note=a.get("note", ""), flags=c.get("flags", {}), order=len(venues), off=off)
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
def vevent_card(e):
    s, n = e["start"], e["end"]
    day = str(s.day) if s == n else (f"{s.day}–{n.day}" if (s.year, s.month) == (n.year, n.month) else str(s.day))
    till = f'<span class="vtill">to {MON[n.month]} {n.day}</span>' if (s.year, s.month) != (n.year, n.month) else ""
    yr = f" {s.year}" if s.year != dt.date.today().year else ""
    price = f'<p class="vnote">{E(e["price_note"])}</p>' if e["price_note"] else ""
    return (f'<article class="vcard" id="v-{e["id"]}"><time class="vdate" datetime="{s.isoformat()}"><span class="vmon">{MON[s.month]}{yr}</span><span class="vday">{day}</span>{till}</time>'
            f'<div class="vbody"><span class="vcat vcat-{e["category"]}">{cat_icon(e["category"])}{CAT_LABEL[e["category"]]}</span><h3>{E(e["title"])}</h3>'
            f'<p class="vloc">{E(e["city"])} · {E(e["venue"])}</p>{price}<p class="vmeta">Checked {dot(e["verified_on"])} · <a href="{E(e["source_url"])}" rel="noopener noreferrer">Source</a></p></div>'
            f'<a class="vcta" href="{E(e["official_url"])}" target="_blank" rel="noopener noreferrer">Official page<span class="vh"> for {E(e["title"])}</span>{NEW_TAB} <span aria-hidden="true">↗</span></a></article>')
PLAT = {"instagram": "Instagram", "youtube": "YouTube", "other": "Profile"}
def initials(name):
    w = [x for x in re.split(r"[\s._-]+", name) if x]
    return "".join(x[0] for x in w[:2]).upper() or "?"
def creator_card(c):
    focus = " · ".join(CAT_LABEL.get(f, f.capitalize()) for f in c["focus"])
    city = f' · {E(c["city"])}' if c["city"] else ""
    return (f'<article class="vcard vcreator" id="c-{c["id"]}"><span class="vinit" aria-hidden="true">{E(initials(c["display_name"]))}</span>'
            f'<div class="vbody"><h3>{E(c["display_name"])}</h3><p class="vloc">{E(c["handle"])} · {PLAT[c["platform"]]}</p>'
            f'<p class="vfocus">{E(focus)}{city}</p><p class="vmeta">Checked {dot(c["verified_on"])}</p></div>'
            f'<a class="vcta" href="{E(c["profile_url"])}" target="_blank" rel="noopener noreferrer">View<span class="vh"> {E(c["display_name"])} on {PLAT[c["platform"]]}</span>{NEW_TAB} <span aria-hidden="true">↗</span></a></article>')
def vevents_section(vevents):
    if not vevents: return ""
    return (f'<section class="sec vsec" aria-labelledby="vev-h"><div class="sec-h"><h2 id="vev-h">Coming up</h2></div>'
            f'<div class="vlist">{"".join(vevent_card(e) for e in vevents)}</div>'
            f'<p class="muted">Official pages only</p></section>')

# ------------------------------------------------------------------ html pieces
CREATORS_LINK = '<a href="/creators/">Creators</a>'
IG_URL = "https://www.instagram.com/hanset.mag/"
IG_SVG = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true" focusable="false"><rect x="3" y="3" width="18" height="18" rx="5"/><circle cx="12" cy="12" r="4"/><circle cx="17.3" cy="6.7" r="1.1" fill="currentColor" stroke="none"/></svg>'
IG_ATTRS = f'href="{IG_URL}" target="_blank" rel="noopener noreferrer" aria-label="HANSET on Instagram @hanset.mag"'

class Site:
    def __init__(self, today, release):
        self.today, self.release = today, release
        self.pages = {}  # path -> html
        self.noindex = set()  # pages kept out of canonical/sitemap even in release (e.g. /join/welcome/)
        self.creators = False

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
                f'<link rel="stylesheet" href="/assets/css/site.css?v={asset_ver("css/site.css")}"></head>')

    def nav(self, active):
        def cur(k): return ' aria-current="page"' if active == k else ""
        return (f'<a class="skip" href="#main">Skip to content</a><header class="site-header"><div class="wrap">'
                f'<div class="hrow"><a class="brand" href="/"><i></i>{BRAND_EN}<small lang="ko">{BRAND_KO}</small></a>'
                f'<div class="hact"><a class="pin" href="/finder/"{cur("finder")}>{FINDER_LABEL}</a><a class="ig" {IG_ATTRS}>{IG_SVG}</a></div></div>'
                f'<nav class="nav" aria-label="Main menu"><a href="/"{cur("home")}>Home</a>'
                f'{MAG_NAV(cur) if ARTICLES else ""}<a href="/guide/"{cur("guide")}>Guide</a><a href="/guides/"{cur("visitors")}>Visitors</a></nav></div></header>')

    def foot(self, active):
        return (f'<footer class="site-footer"><div class="wrap"><strong>{BRAND_EN}</strong> · Train. Sweat. One set.'
                f'<div class="flinks"><a href="/join/">Join</a><a href="/about/">About</a><a href="/guide/">Guide</a><a href="/archive/">Venue Sketches</a><a href="/events/">Events</a>{CREATORS_LINK if self.creators else ""}<a href="/hyrox-seoul/">HYROX Seoul</a><a href="/faq/">FAQ</a><a href="/corrections/">Corrections</a>'
                f'<a href="/privacy/">Privacy</a></div>'
                f'<p class="fsocial"><a class="ig ig-f" {IG_ATTRS}>{IG_SVG}<span>@hanset.mag</span></a></p>'
                f'<p>Prices and closing days change. Every entry shows its check date and source. Confirm with the venue before you go.</p>'
                f'<p>© 2026 {BRAND_EN}</p></div></footer>')

    def add(self, path, title, desc, body, active="", scripts=(), noindex=False):
        if noindex: self.noindex.add(path)
        sc = "".join(f'<script src="/assets/js/{s}?v={asset_ver("js/"+s)}" defer></script>' for s in scripts)
        out_path = path
        self.pages[out_path] = (self.head(title, desc, path, noindex) + f'<body>{self.nav(active)}<main id="main" class="wrap">{body}</main>{self.foot(active)}{sc}</body></html>')

def clip(t, n=160):
    """Meta description: never cut inside a word or number; prefer a sentence end."""
    if len(t) <= n: return t
    cut = t[:n]
    end = cut.rfind(". ")
    return cut[:end + 1] if end > n // 2 else cut[:cut.rfind(" ")].rstrip(" ,·—(") + "…"
def badge(txt, cls=""): return f'<span class="badge {cls}">{E(txt)}</span>'
def v_badges(v, full=False):
    b = []
    if v["ad"]: b.append(badge("Ad", "badge-ad"))
    b.append(badge("Box" if v["kind_key"] == "box" else "Sauna", "badge-box" if v["kind_key"] == "box" else "badge-sauna"))
    b.append(badge(v["kind"]))
    if v["c"].get("opening_soon"): b.append(badge("Opening soon", "badge-part"))
    b.append(badge("Verified" if v["status"] == "verified" else "Partly verified", "badge-ok" if v["status"] == "verified" else "badge-part"))
    if v["stale"]: b.append(badge("오래됨 (outdated)", "badge-old"))
    for t in v["tags"]: b.append(badge(t))
    return '<div class="chips">' + "".join(b) + "</div>"
def off_marks(v):
    o = v["off"]; out = []
    if o["cf"]: out.append(f'CrossFit affiliate verified ({o["date"]})')
    if o["hyrox"]: out.append(f'HYROX official gym list verified ({o["date"]})')
    return out
def off_html(v):
    m = off_marks(v)
    return "".join(f'<div class="off">✓ {E(t)}</div>' for t in m)
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
    return [("Hours & Closed", hours), ("Access / Booking", access), ("Visitor / Tattoo rules", rules)]


def fact_provenance(v, value):
    """A venue reply is not evidence available at the representative website."""
    if value.startswith(UNKNOWN) or value.lower() in ("pending", "not confirmed", "not yet announced"):
        return ""
    citations = re.findall(
        r"\((?:official page and )?(?:venue|hotel) reply\b[^()]*\)"
        r"|(?:—|--)\s*(?:venue|hotel) reply\b[^\n()]*", value, re.I)
    if citations:
        # Dates outside an explicit reply citation may be closures or inquiry dates.
        dates = [date for citation in citations for date in
                 re.findall(r"\b20\d{2}[-.]\d{2}[-.]\d{2}\b|\b\d{1,2}/\d{1,2}\b", citation)]
        if dates or not any(c.lower().startswith("(official page and ") for c in citations):
            checked = ", ".join(dict.fromkeys(dates)) if dates else "date not confirmed"
            return f'<small class="fact-source">Venue reply · {E(checked)} · Public source: {UNKNOWN}</small>'
    return (f'<small class="fact-source">Checked {dot(v["last_verified"])} · '
            f'<a href="{E(v["source"])}" rel="noopener noreferrer">Official source</a></small>')


def fact_lines(rows, v=None):
    return "".join(f'<span class="fact-line"><strong>{E(k)}:</strong> {E(val)}'
                   f'{fact_provenance(v, val) if v else ""}</span>' for k, val in rows) or f'<span class="unk">{UNKNOWN}</span>'


def price_html(v):
    # The numeric CSV value is a sortable hint, not a replacement for the terms.
    context = [(k, val) for k, val in v["facts"] if
               (k.startswith("Price") or k.startswith("Drop-in") or k in
                ("Group session", "Open gym", "Traveler pass", "Trial", "Guest discount rate", "Jjimjil", "Jjimjil wear"))
               and val.strip() and val != PENDING]
    numeric = (f'<div class="price"><span class="price-label">{E(v["price_label"])}:</span> '
               f'<strong>{E(v["price"])}</strong></div>{cav(v)}') if v["price"] else ""
    terms = fact_lines(context, v) if context else ("" if numeric else f'<span class="unk">Price: {UNKNOWN}</span>')
    return numeric + '<div class="price price-context">' + terms + '</div>'


def price_short(v):
    # List cards: headline price only. Full terms stay on the venue page.
    return (f'<div class="price"><span class="price-label">{E(v["price_label"])}:</span> <strong>{E(v["price"])}</strong></div>'
            if v["price"] else f'<span class="unk">Price: {UNKNOWN}</span>')

def venue_facts(v, preview=False):
    groups = []
    for label, rows in fact_groups(v):
        essential, extra = [], []
        for key, value in rows:
            # Keep access, tattoo, age, capacity and time limits visible in full.
            target = extra if (label == "Hours & Closed" and len(value) > 160) or key in (
                "House rules", "Open-air bath rules", "Body scrub", "English support", "Language") else essential
            if preview and label == "Hours & Closed":
                target = extra
            if access_conflict(v) and key in ("Who can use", "Non-guests", "Entry"):
                target = extra
            if key == "Entry restriction" and any(value == val for k, val in essential):
                continue
            if key != "Tattoos" and any(value == val for k, val in essential if k == "Tattoos"):
                continue
            target.append((key, value))
        shown = fact_lines(essential, v) if essential else ""
        if extra:
            shown += f'<details class="fact-extra"><summary>Full {E(label.lower())} · source notes</summary>{fact_lines(extra, v)}</details>'
        groups.append(f'<div><dt>{E(label)}</dt><dd>{shown or fact_lines([])}</dd></div>')
    return '<dl class="card-facts">' + "".join(groups) + '</dl>'


def venue_contents(v, preview=False):
    title = E(v["title"]) if preview else f'<a href="/finder/{v["slug"]}/">{E(v["title"])}</a>'
    footer = (f'<div class="meta">Checked {dot(v["last_verified"])} · Official reference '
              f'<a href="{E(v["source"])}" rel="noopener noreferrer">{E(host(v["source"]))}</a></div>')
    actions = (f'<div class="btnrow"><a class="btn btn-d" href="/finder/{v["slug"]}/">View full details →</a>'
               f'<a class="btn map-link" href="{E(map_url(v))}" rel="noopener noreferrer">Open in map ↗</a></div>' if preview else
               f'<a class="card-details" href="/finder/{v["slug"]}/">Details &amp; facts →</a>')
    return (f'{v_badges(v)}<h3>{title}</h3><div class="loc">{E(v["location"])}</div>{price_short(v)}'
            + f'{footer}{actions}')

def venue_card(v):
    attrs = (f'data-slug="{v["slug"]}" data-city="{v["city_key"]}" data-type="{v["kind_key"]}" data-sunday="{v["sunday"]}" data-price="{1 if v["price"] else 0}" data-cf="{1 if v["off"]["cf"] else 0}" data-hyrox="{1 if v["off"]["hyrox"] else 0}" data-sub="{v["sub"]}" data-order="{v["order"]}" data-date="{v["last_verified"].isoformat()}" data-title="{E(v["title"])}" '
             f'data-name="{E((v["title"] + " " + v["name"] + " " + v["location"] + " " + v["area"] + " " + v["kind"] + " " + v["title_ko"] + " " + v["location_ko"]).lower())}"')
    return f'<article class="card venue-card" {attrs}>{venue_contents(v)}</article>'

# Presentation coordinates for schematic regions only, never entrance coordinates.
# Membership comes solely from existing location/area text. Unknowns are off-map.
# Schematic lattice: pins sit on a 54x66 grid so 52-unit hit areas never overlap
# (>=44px on a 390px phone) and labels sit in the gap above each row.
MAP_PITCH_X, MAP_PITCH_Y, MAP_TOP, MAP_LEFT, MAP_HIT = 54, 66, 58, 38, 52
MAP_RIVER_ROW = 2  # Seoul: the Han River runs just above this row
MAP_RIVER_Y = MAP_TOP + MAP_RIVER_ROW * MAP_PITCH_Y - 35
# Each row: (group key, label or "" for continuation, columns); key None = empty cell.
MAP_LAYOUT = {
    # Schematic, not to scale. Rows 0-1 sit north of the Han River, rows 2-5 south (MAP_RIVER_ROW).
    # Columns follow rough real west-to-east order (district-office longitude); a district's mean
    # pin x is what tests rank. Only districts that currently have venues get a slot; add a slot
    # here when a venue opens in a new district (tests know all 25 districts' side and longitude).
    "seoul": [[("Mapo", "Mapo", 2), ("Yongsan", "Yongsan", 1), ("Jung-gu", "Jung-gu", 1), ("Gangbuk", "Gangbuk", 1), ("Nowon", "Nowon", 1)],
              [(None, "", 1), ("Yongsan", "", 1), ("Jung-gu", "", 3), ("Gwangjin", "Gwangjin", 2)],
              [("Gangseo", "Gangseo", 1), ("Dongjak", "Dongjak", 1), (None, "", 1), ("Seocho", "Seocho", 3), ("Gangdong", "Gangdong", 1)],
              [("Yeongdeungpo", "Yeongdeungpo", 2), ("Gangnam", "", 1), (None, "", 1), ("Seocho", "", 2), ("Gangdong", "", 1)],
              [(None, "", 1), ("Gwanak", "Gwanak", 1), (None, "", 1), ("Gangnam", "Gangnam / Sinsa", 3), ("Gangdong", "", 1)],
              [(None, "", 1), ("Gwanak", "", 1), ("Gangnam", "", 4), ("Songpa", "Songpa", 1)]],
    "busan": [[("Dongnae", "Dongnae", 1), ("Haeundae", "Haeundae", 3), ("Nam-gu", "Nam-gu", 1), ("Jung-gu", "Jung-gu", 1), ("Seo-gu", "Seo-gu", 1)],
              [(None, "", 3), ("", "District not confirmed · off-map", 1)]],
    "jeju": [[("Jeju-si", "Jeju-si", 2), (None, "", 2), ("Seogwipo", "Seogwipo / Jungmun", 3)]],
    "gyeonggi": [[("Goyang", "Goyang / Samsong", 3), (None, "", 1), ("Icheon", "Icheon", 2)]],
    "all": [[("seoul", "Seoul", 7)], [("seoul", "", 7)], [("seoul", "", 7)], [("seoul", "", 7)], [("seoul", "", 7)],
            [("gyeonggi", "Gyeonggi", 2), ("busan", "Busan", 5)],
            [("jeju", "Jeju", 4), ("busan", "", 3)]],
}


def map_lattice(city):
    """Return (labels, slots per key, unknown-box cells, viewBox height)."""
    labels, slots, unknown = [], {}, []
    rows = MAP_LAYOUT[city]
    for r, row in enumerate(rows):
        y, col = MAP_TOP + r * MAP_PITCH_Y, 0
        for key, label, cols in row:
            xs = [MAP_LEFT + (col + i) * MAP_PITCH_X for i in range(cols)]
            col += cols
            if key is None:
                continue
            slots.setdefault(key, []).extend((x, y) for x in xs)
            if label:
                # Seoul row just south of the river: drop labels 7 units so the river line clears them.
                drop = 7 if city == "seoul" and r == MAP_RIVER_ROW else 0
                labels.append((label, (xs[0] + xs[-1]) / 2, y - 23 + drop, key))
            if key == "":
                unknown.append((xs[0], y))
        if col > 7:
            raise ValueError(f'{city} map row {r} has {col} columns; max 7')
    return labels, slots, unknown, MAP_TOP + (len(rows) - 1) * MAP_PITCH_Y + 34


MAP_DISTRICTS = {city: [key for row in rows for key, label, _ in row if key is not None and (label or key == "")]
                 for city, rows in MAP_LAYOUT.items() if city != "all"}
def map_zones(city):
    """All Korea view: outline each region's pin block so the grid reads as regions, not one icon wall."""
    if city != "all":
        return ""
    blocks = []  # [key, x0, x1, y0, y1]; same-key same-span segments in consecutive rows merge
    for r, row in enumerate(MAP_LAYOUT[city]):
        y, col = MAP_TOP + r * MAP_PITCH_Y, 0
        for key, _, cols in row:
            x0, x1 = MAP_LEFT + col * MAP_PITCH_X, MAP_LEFT + (col + cols - 1) * MAP_PITCH_X
            col += cols
            if key is None:
                continue
            prev = next((b for b in blocks if b[0] == key and b[1] == x0 and b[2] == x1 and b[4] == y - MAP_PITCH_Y), None)
            if prev:
                prev[4] = y
            else:
                blocks.append([key, x0, x1, y, y])
    return "".join(f'<rect class="map-zone map-zone-{k}" x="{x0-25}" y="{y0-38}" width="{x1-x0+50}" height="{y1-y0+64}" rx="14"/>'
                   for k, x0, x1, y0, y1 in blocks)


def map_drawing(city, h):
    land = f'<rect class="map-land" x="8" y="8" width="384" height="{h-16}" rx="28"/>' + map_zones(city)
    if city == "seoul":
        y = MAP_RIVER_Y  # between the north and south rows
        land += f'<path class="map-river" d="M8 {y}C80 {y-10} 140 {y+10} 200 {y}S320 {y-10} 392 {y}"/><text class="river-label" x="56" y="{y-9}" text-anchor="middle">Han River</text>'
    return land


def city_tabs(map_header=False):
    items = [("", "All Korea" if map_header else "All"), ("seoul", "Seoul"), ("busan", "Busan"), ("jeju", "Jeju"), ("gyeonggi", "Gyeonggi")]
    return '<div class="seg map-tabs" data-seg="city" role="group" aria-label="Map city">' + "".join(
        f'<button type="button" data-v="{key}" aria-pressed="{str(not key).lower()}">{label}</button>' for key, label in items) + '</div>'


def pin_glyph(kind):
    if kind == "box":
        return '<rect class="pin-shape" x="-11" y="-11" width="22" height="22" rx="2"/><path class="pin-glyph" d="M-7 0H7M-7-4V4M-4-6V6M7-4V4M4-6V6"/>'
    return '<circle class="pin-shape" r="11"/><path class="pin-glyph" d="M-7 6a7 7 0 0 1 14 0M-4-6q-2 2 0 4M1-7q-2 2 0 4M6-6q-2 2 0 4"/>'


def map_pin(v, x, y, label):
    return (f'<g class="map-pin map-pin-{v["kind_key"]}" role="button" tabindex="0" '
            f'aria-label="{E(v["title"])} · {E(label)} · {E(v["kind"])}" aria-pressed="false" aria-controls="map-preview" '
            f'data-pin="{v["slug"]}" transform="translate({x} {y})"><title>{E(v["title"])}</title>'
            f'<rect class="pin-target" x="-26" y="-26" width="52" height="52"/>{pin_glyph(v["kind_key"])}</g>')


def venue_map(venues):
    panels = []
    city_venues = {city: sorted((v for v in venues if v["city_key"] == city), key=lambda v: v["order"]) for city in MAP_DISTRICTS}
    for city in ("all", *MAP_DISTRICTS):
        labels, slots, unknown, h = map_lattice(city)
        drawing = map_drawing(city, h)
        for x, y in unknown:
            drawing += f'<rect class="map-unknown" x="{x-27}" y="{y-40}" width="54" height="68" rx="8"/>'
        if city == "all":
            groups = city_venues
        else:
            groups = {key: [] for key in MAP_DISTRICTS[city]}
            for v in city_venues[city]:
                location = (v["location"] + " " + v["area"]).lower()
                key = next((key for key in groups if key and key.lower() in location), "")
                if key not in groups:
                    raise ValueError(f'Unknown region for {v["slug"]}; add an off-map area')
                groups[key].append(v)
        for label, x, y, key in labels:
            if city == "all" or key == "" or groups.get(key):  # only districts with venues are drawn
                drawing += f'<text class="map-label" x="{x:g}" y="{y}">{E(label)}</text>'
        for key, items in groups.items():
            if len(items) > len(slots.get(key, [])):
                raise ValueError(f'Expand schematic slots for {city}/{key}; never overlap pins')
            place = (key.title() + " · schematic region only") if city == "all" else next(l for l, *_ in labels if l.split(" /")[0].startswith(key or "District")) if key else "District not confirmed · off-map"
            for v, (x, y) in zip(items, slots[key]):
                drawing += map_pin(v, x, y, place)
        label = "All Korea" if city == "all" else city.title()
        panels.append(f'<svg class="district-map" data-map-city="{city}" viewBox="0 0 400 {h}" role="group" '
                      f'aria-label="{label} schematic venue map"' + (' hidden="hidden"' if city != "all" else '') + f'>{drawing}</svg>')
    boxes = sum(v["kind_key"] == "box" for v in venues)
    legend = "".join(f'<span><svg viewBox="-20 -20 40 40" class="legend-pin map-pin-{kind}" aria-hidden="true">{pin_glyph(kind)}</svg>{label} ({count})</span>' for kind, label, count in (("box", "Box", boxes), ("sauna", "Sauna", len(venues)-boxes)))
    templates = "".join(f'<template id="preview-{v["slug"]}"><article class="selected-venue">{venue_contents(v, preview=True)}</article></template>' for v in venues)
    return (f'<section class="venue-map" aria-labelledby="map-heading"><div class="map-header"><h2 id="map-heading">Explore the map <span id="map-view">All Korea</span></h2>'
            f'{city_tabs(True)}<div class="map-legend" aria-label="Map legend">{legend}</div></div>'
            f'<div class="map-regions">{"".join(panels)}</div>'
            '<p class="map-disclaimer">Schematic map · not exact locations</p></section>'
            '<div id="map-selection" class="map-selection"><div id="map-preview" aria-live="polite" aria-atomic="true"><p>Select a pin to preview.</p></div></div>' + templates)


def mini_card(v):
    price = (f'{E(v["price_label"])} {E(v["price"])}' if v["price"] else "Price pending")
    return (f'<article class="card">{v_badges(v)}<h3><a href="/finder/{v["slug"]}/">{E(v["title"])}</a></h3>'
            f'<div class="loc">{E(v["location"])}</div><div class="price{"" if v["price"] else " unk"}">{price}</div>{off_html(v)}'
            f'<div class="meta">Checked {dot(v["last_verified"])}</div></article>')

def event_card(e, today, detail_link=True):
    d = (e["start"] - today).days
    badge_txt = "Live now" if e["start"] <= today <= e["end"] else ("This week" if 0 <= d <= 7 else "")
    rows = "".join(f'<tr><th>{E(k)}</th><td>{E(val)}</td></tr>' for k, val in e["facts"])
    link = f'<a href="/hyrox-seoul/">Race week guide →</a> · ' if e["feature"] and detail_link else ""
    return (f'<article class="card" id="{e["slug"]}" data-end="{e["end"].isoformat()}"><div class="chips">{badge(e["kind"])}{badge(badge_txt, "badge-part") if badge_txt else ""}</div>'
            f'<h3>{E(e["title"])}</h3><div class="when" data-dday="{e["start"].isoformat()}">{span(e["start"], e["end"], today.year)}{(" · <b>" + ("D-DAY" if d == 0 else f"D-{d}") + "</b>") if d >= 0 else ""}</div>'
            f'<table class="facts"><tbody>{rows}</tbody></table>' + (f'<p class="muted">{E(e["note"])}</p>' if e["note"] else "") +
            f'<div class="meta">{link}<a href="{E(e["url"])}" rel="noopener noreferrer">{E(e["source_label"])}</a> · Checked {dot(e["last_verified"])}</div></article>')

# ------------------------------------------------------------------ pages
HYROX_STATIONS = [("SkiErg", "1,000m"), ("Sled Push", "50m"), ("Sled Pull", "50m"),
                  ("Burpee Broad Jump", "80m"), ("Rowing", "1,000m"), ("Farmers Carry", "200m"),
                  ("Sandbag Lunges", "100m"), ("Wall Balls", "100 reps")]


# ------------------------------------------------------------------ magazine (articles)
MAG_NAV = lambda cur: f'<a href="/magazine/"{cur("magazine")}>Magazine</a>'
ARTICLES = [  # newest first; source markdown lives in content/articles/. Empty = magazine hidden (nav, home, /magazine/ not built).
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

BYLINE = 'Editor Cheon Han-saem (<span lang="ko">천한샘</span>)'

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
    feat = next(e for e in events if e["feature"])
    upcoming = [e for e in events if e["end"] >= today]
    ordered = sorted(venues, key=lambda v: (not v["ad"], not bool(v["price"]), v["status"] != "verified", -len(v["facts"]), v["order"]))
    cnt_box = sum(v["kind_key"] == "box" for v in venues); cnt_sauna = len(venues) - cnt_box
    seg = lambda g, items: '<div class="seg" data-seg="%s" role="group" aria-label="%s">' % (g, g.title()) + "".join(f'<button type="button" data-v="{k}" aria-pressed="false">{E(t)}</button>' for k, t in items) + "</div>"
    explorer = f'''
{venue_map(ordered)}
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
<div class="grid" id="finder-list">{"".join(venue_card(v) for v in ordered)}</div>
<div class="empty" id="f-empty" hidden><p>Nothing here yet.</p><p class="muted">Try fewer filters. More verified places are on the way.</p><button type="button" class="btn btn-d" data-reset>Start over</button></div>
<div class="note">"Verified" means we read the info directly on the venue’s official page. "Partly verified" means only some items, like the address. Sunday is filled in only when the official page says so. "CrossFit affiliate verified" and "HYROX official gym list verified" appear only where we saw the place ourselves in that official page or list on the check date. No mark means still checking.</div>
<noscript><p class="muted">Filters need JavaScript. The full list still works without it.</p></noscript>'''
    # ---- home
    race_strip = ""
    if feat["end"] >= today:
        d = max((feat["start"] - today).days, 0)
        started = today >= feat["start"]
        btxt = "Live now" if started else ("D-DAY" if d == 0 else f"D-{d}")
        ds = f'{feat["start"].month}/{feat["start"].day}~{feat["end"].month}/{feat["end"].day}'
        race_strip = (f'<div class="race-strip" data-end="{feat["end"].isoformat()}">'
                      f'<a class="race-strip-link race-strip-narrow" href="/hyrox-seoul/"><span>HYROX Seoul</span><span>·</span>'
                      f'<b data-dday="{feat["start"].isoformat()}">{btxt}</b><span>·</span><span>{ds}</span><span>·</span><span>Guide ›</span></a>'
                      f'<a class="race-strip-link race-strip-wide" href="/hyrox-seoul/"><span>HYROX Seoul</span><span>·</span>'
                      f'<b data-dday="{feat["start"].isoformat()}">{btxt}</b><span>·</span><span>{ds}</span><span>·</span><span>KINTEX</span><span>·</span><span>Guide ›</span></a>'
                      f'</div>')
    home_mag = (f'''<section class="sec home-mag"><div class="sec-h"><h2>Read first. Every claim cited.</h2><a href="/magazine/">All articles ›</a></div>
<div class="grid">{"".join(f'<article class="card"><div class="chips">{badge(a["category"])}</div><h3><a href="/magazine/{a["slug"]}/">{E(a["title_en"])}</a></h3><div class="meta">{BYLINE} · {dot(dt.date.fromisoformat(a["date"]))}{"" if a.get("lang") == "en" else " · Article in Korean"}</div></article>' for a in [parse_article(m) for m in ARTICLES])}</div></section>''' if ARTICLES else '')
    guide_rows = "".join(f'<li><a href="/guides/{s}/">{E(t)}</a></li>' for s, t, _ in GUIDES)
    body = f'''
<section class="hero home-hero"><span class="home-kicker">Wellness in Korea, for visitors</span><h1>Can I go?</h1>
<div class="hero-ctas"><a class="btn btn-ghost" href="/guides/">Visitors guide</a></div></section>

<section class="sec home-results"><h2>Explore {len(venues)} boxes &amp; saunas</h2>
{explorer}</section>

<section class="sec home-guides"><h2>Visitors guide</h2>
<ul class="guide-list">{guide_rows}</ul></section>

{race_strip}
<p class="safety-line">{SAFETY}</p>

{home_mag}

<div class="slot" data-slot="kix-ticketing" hidden></div>
<section class="sec"><div class="sec-h"><h2>Floor by floor.</h2><a href="/archive/">Archive ›</a></div>
<div class="illrow arcrow">{"".join(f'<a class="illc" href="/archive/#{vs}"><picture><source media="(min-width:720px)" srcset="/assets/img/archive/{f}-mid.png"><img src="/assets/img/archive/{f}-thumb.png" width="252" height="315" loading="lazy" decoding="async" alt="{E(d)}"></picture><span><b>{E(st)}</b><em>View ›</em></span></a>' for f, vs, t, st, d, sl, su in ARCHIVE)}</div>
<p><a class="btn btn-d" href="/archive/">See every drawing ›</a></p></section>'''
    site.add("/", f"{BRAND_EN} — CrossFit, HYROX & Sauna Wellness Magazine in Korea", "CrossFit, HYROX, hotel and boutique saunas in Korea. A drop-in and sauna finder plus race dates. Every entry with an official source and check date.", body, "home", ("dday.js", "finder.js"))

    # ---- finder
    fbody = f'''
<div class="finder-head"><h1>{FINDER_LABEL}</h1>
<p class="lead"><b>{cnt_box}</b> boxes · <b>{cnt_sauna}</b> saunas</p></div>
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
                cell = f'<td class="unk">{PENDING}</td>' if not val.strip() else f'<td>{E(val)}{fact_provenance(v, val)}</td>'
                out.append(f'<tr><th>{E(k)}</th>{cell}</tr>')
            return "".join(out)
        links = []
        if v["website"] and "map.naver.com" not in v["website"]:
            links.append(f'<a class="btn btn-d" href="{E(v["website"])}" rel="{"sponsored " if v["ad"] else ""}noopener noreferrer">Official site</a>')
        if v["booking"] and v["booking"] != v["website"]:
            links.append(f'<a class="btn btn-d" href="{E(v["booking"])}" rel="noopener noreferrer">Book</a>')
        links.append(f'<a class="btn btn-d" href="{E(map_url(v))}" rel="noopener noreferrer">Open in map</a>')
        srcs = [f'<a href="{E(v["source"])}" rel="noopener noreferrer">{E(host(v["source"]))}</a>'] + [f'<a href="{E(u)}" rel="noopener noreferrer">{E(l)}</a>' for l, u in v["extra_sources"]]
        adnote = (f'<div class="note ad"><strong>Ad</strong> · This entry is labeled as an ad. Verified info still comes from official sources. '
                  f'{E(v["ad_note"])}</div>' if v["ad"] else "")
        tips = ("<h2>Tips · Heads-up</h2><ul class='clean'>" + "".join(f"<li>{E(t)}</li>" for t in v["tips"]) + "</ul>") if v["tips"] else ""
        unv = ("<h2>Not yet verified</h2><ul class='clean'>" + "".join(f"<li>{E(t)}</li>" for t in v["unverified"]) + "</ul>") if v["unverified"] else ""
        stale = (f'<div class="note">오래됨 (outdated) · Last checked over {STALE_DAYS} days ago. Confirm with the venue before you go.</div>' if v["stale"] else "")
        safety = f'<p class="safety">{SAFETY}</p>' if v["kind_key"] == "sauna" else ""
        offrows = "".join(f'<tr><th>Official list</th><td>{E(t)}</td></tr>' for t in off_marks(v))
        extra_addr = f'<tr><th>Address</th><td><span lang="ko">{E(v["address"])}</span></td></tr>' if v["address"] and not any(k == "주소" for k, _ in v["facts_ko"]) else ""
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
{v_badges(v)}<h1>{E(v["title"])}</h1><p class="muted">{E(v["location"])}</p>{adnote}{stale}
<div class="btnrow detail-actions">{"".join(links)}</div>
<section class="detail-glance" aria-labelledby="glance-heading"><h2 id="glance-heading">At a glance</h2>
<div class="glance-grid"><div><h3>Price</h3>{price_html(v)}</div>{"".join(f'<div><h3>{E(label)}</h3>{fact_lines(values, v)}</div>' for label, values in fact_groups(v))}</div>
<p class="meta">Checked {dot(v["last_verified"])} · Official reference <a href="{E(v["source"])}" rel="noopener noreferrer">{E(host(v["source"]))}</a></p></section>
<table class="facts"><tbody>{rows()}{extra_addr}{sunrow}{closed_row(v)}{offrows}</tbody></table>
{tips}{unv}{fchk(v)}{aer_blk}{arch_blk}
<div class="verify"><p><strong>Last checked {dot(v["last_verified"])}</strong></p><p>Source: {" · ".join(srcs)}</p>
<p><a href="/corrections/">Report a correction</a></p></div>
{next_html}
<div class="btnrow">{"".join(links)}</div>{safety}</div>'''
        site.add(f"/finder/{v['slug']}/", f"{v['title']}", clip(f"{v['title']} — {v['summary']}"), body, "finder")

    # ---- events
    cards = "".join(event_card(e, today) for e in upcoming)
    if not upcoming: cards = '<div class="empty"><p>No officially confirmed races right now.</p><p class="muted">We add them as they’re confirmed.</p></div>'
    ebody = f'''<div class="page"><h1>Event Calendar</h1></div>
<div class="ev">{cards}</div>
{vevents_section(vevents)}
<div class="note">Races listed only on aggregator sites wait until the organizer confirms. Fees appear only when read on the official page. <a href="/events.ics">Download all dates (.ics)</a></div>'''
    site.add("/events/", "Event Calendar", "HYROX Seoul, Urban Wave, the CrossFit Open and more. Race dates confirmed on official pages.", ebody, "events", ("dday.js",))

    # ---- creators (verified feed; page only when there is data)
    if creators:
        cbody = (f'<div class="page"><h1>Creators</h1></div>'
                 f'<section class="sec vsec" aria-label="Creators"><div class="vlist">{"".join(creator_card(c) for c in creators)}</div>'
                 f'<p class="muted">Public profiles · dated</p></section>')
        site.add("/creators/", "Creators", "CrossFit, HYROX, running and sauna creators in Korea. Public profiles, checked by date.", cbody, "creators")

    # ---- HYROX feature
    trs = "".join(f"<tr><td>{i}</td><td>1km</td><td>{E(en)}</td><td>{E(d)}</td></tr>" for i, (en, d) in enumerate(HYROX_STATIONS, 1))
    hv = "".join(mini_card(v) for v in hyrox_v)
    hbody = f'''<section class="hero"><span class="eyebrow">RACE WEEK GUIDE</span><h1>{E(feat["title"])}</h1>
<div class="dday" data-dday="{feat["start"].isoformat()}"><b>D-{max((feat["start"] - today).days, 0)}</b>{span(feat["start"], feat["end"], today.year)} · KINTEX</div>
<p>First race? Start here. Dates from the official page.</p>
<div class="btnrow"><a class="btn btn-a" href="{E(feat["url"])}" rel="noopener noreferrer">Registration and fees: official page</a></div></section>
<div class="page"><h2>At a glance</h2>
<table class="facts"><tbody>{"".join(f"<tr><th>{E(k)}</th><td>{E(val)}</td></tr>" for k, val in feat["facts"])}</tbody></table>
<h2>The race</h2>
<p>1km run, one functional station. One set. Eight times. Order and distances below.</p>
<table class="t"><thead><tr><th>#</th><th>Run</th><th>Station</th><th>Distance · reps</th></tr></thead><tbody>{trs}</tbody></table>
<h2>Pick your division</h2>
<ul><li>Open / Pro (solo)</li><li>Doubles / Doubles Pro (pairs)</li><li>Relay: teams of 4, each runs 2×1km and one station</li></ul>
<div class="note">Weights by division and gender are on the official page. Standards can change by season, so recheck the <a href="https://hyrox.com/the-fitness-race/" rel="noopener noreferrer">official HYROX guide</a> before you race. Registration status and fees weren’t in the official page text, so they're not listed.</div>
<h2>Before race day, train here.</h2><p>Places with HYROX classes or drop-ins confirmed on their official pages. Prices only when verified.</p></div>
<div class="grid g3">{hv}</div>
<div class="page"><p class="muted">Saunas and boxes around Goyang and Ilsan go into the <a href="/finder/?city=gyeonggi">Finder</a> as we verify them.</p>
<p class="muted">{SAFETY}</p><p class="muted">Checked {dot(feat["last_verified"])} · Source <a href="{E(feat["url"])}" rel="noopener noreferrer">HYROX Korea</a> · <a href="https://hyrox.com/the-fitness-race/" rel="noopener noreferrer">HYROX official</a></p></div>'''
    site.add("/hyrox-seoul/", "HYROX Seoul Race Week Guide (11/13–15, KINTEX)", "AirAsia HYROX Seoul dates, the 8 stations, and drop-in spots to train first.", hbody, "hyrox", ("dday.js",))


    # ---- magazine
    arts = [parse_article(m) for m in ARTICLES]
    for a in arts:  # magazine pages only when there are articles
        site.add(f"/magazine/{a['slug']}/", a["title_en"], clip(a["deck_en"]), article_html(a), "magazine")
    mcards = "".join(f'<article class="card"><div class="chips">{badge(a["category"])}</div><h3><a href="/magazine/{a["slug"]}/">{E(a["title_en"])}</a></h3>'
                     f'<div class="loc">{E(a["deck_en"])}</div><div class="meta">{BYLINE} · {dot(dt.date.fromisoformat(a["date"]))}{"" if a.get("lang") == "en" else " · Article in Korean"}</div></article>' for a in arts)
    if arts: site.add("/magazine/", "Magazine", "Wellness reports from HANSET editors. Research and official sources, made easy to read.",
             f'<div class="page"><h1>Magazine</h1><p class="muted">Articles are currently in Korean.</p></div><div class="grid">{mcards}</div>', "magazine")

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

    guides(site, venues)
    site.add("/404.html", "Page not found", "This page doesn’t exist.", f'<div class="page"><h1>Lost? Let’s get you back.</h1><p>This page doesn’t exist. Start again from <a href="/">Home</a> or the <a href="/finder/">{FINDER_LABEL}</a>.</p></div>')

def asset_ver(rel):
    import hashlib
    return hashlib.md5((ROOT / "static" / rel).read_bytes()).hexdigest()[:8]

ILLUSTRATIONS = [
    ("box", "crossfit-box-plan", "Inside a CrossFit box", "Hand-drawn plan of a typical CrossFit box: whiteboard, WOD floor with kettlebells and boxes, rig, open gym with barbell racks, lockers, rowers and bikes, front desk and entrance."),
    ("dropin", "dropin-steps", "How a drop-in works", "Hand-drawn drop-in steps: 1 check class times and price online, 2 book or message the box, 3 say drop-in at the desk, 4 train, 5 put gear back and pay."),
    ("sauna", "sauna-guide", "How to sauna", "Hand-drawn sauna plan and order: lockers, shower, dry or wet sauna, optional cold plunge, rest and water. Skip it if pregnant, unwell, after drinking or with heart issues."),
]

def ics(events):
    def esc(s): return s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//HANSET//events//EN", "CALSCALE:GREGORIAN", "X-WR-CALNAME:HANSET Events"]
    for e in [x for x in events if x["end"] >= dt.date.today()]:
        lines += ["BEGIN:VEVENT", f"UID:{e['slug']}@hanset", f"DTSTAMP:{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}",
                  f"DTSTART;VALUE=DATE:{e['start']:%Y%m%d}", f"DTEND;VALUE=DATE:{(e['end'] + dt.timedelta(days=1)):%Y%m%d}",
                  f"SUMMARY:{esc(e['title'])}", f"LOCATION:{esc(e['venue'] + ', ' + e['city'])}", f"URL:{e['url']}",
                  "DESCRIPTION:" + esc("Dates from the official page. Check fees and registration on the official page."), "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"

def subset_fonts(dist):
    try:
        from fontTools import subset
        from fontTools.ttLib import TTFont
    except ImportError:
        print("WARN: fonttools not installed; skipping font subsetting (site falls back to system fonts). Use .venv/bin/python.")
        return
    text = "".join(p.read_text(encoding="utf-8") for p in dist.rglob("*.html"))
    text += "".join(p.read_text(encoding="utf-8") for p in (dist / "assets/js").glob("*.js"))
    chars = "".join(sorted(set(text + "".join(chr(c) for c in range(0x20, 0x7F)) + "₩–—·→›×℃™…“”‘’")))
    out = dist / "assets/fonts"; out.mkdir(parents=True, exist_ok=True)
    for w in ("Regular", "Bold"):
        opts = subset.Options(); opts.flavor = "woff2"; opts.layout_features = ["*"]; opts.name_IDs = ["*"]; opts.notdef_outline = True
        f = TTFont(ROOT / f"static/fonts/Pretendard-{w}.otf")
        s = subset.Subsetter(opts); s.populate(text=chars); s.subset(f)
        f.flavor = "woff2"; f.save(out / f"Pretendard-{w}.subset.woff2")
    shutil.copy(ROOT / "static/fonts/Pretendard-OFL-LICENSE.txt", out / "Pretendard-OFL-LICENSE.txt")

def leak_check(dist):
    bad = []
    for p in list(dist.rglob("*.html")) + list(dist.rglob("*.ics")) + list(dist.rglob("*.xml")):
        t = p.read_text(encoding="utf-8")
        for pat in FORBIDDEN:
            m = re.search(pat, t)
            if m: bad.append((str(p.relative_to(dist)), pat, t[max(0, m.start() - 20):m.end() + 20]))
    if re.search(r"https?://[^\"' >]*(google-analytics|googletagmanager|facebook\.net|doubleclick|hotjar|clarity\.ms|kakao\.com/js)", "".join(p.read_text(encoding="utf-8") for p in dist.rglob("*.html"))):
        bad.append(("*", "tracker", ""))
    return bad

ARCHIVE_DATE = "2026.10.18"
# (file, venue slug, title, short title, alt text, source label, source URL)
ARCHIVE = [
    ("archive-sisu-house", "sisu-house", "Sisu House, floor by floor", "Sisu House", "Hand drawing stacking the floors of Sisu House (1F Welcoming Zone, 1.5F Touch Zone, 2-3F Private Zone) and its ritual sequence.", "sisu.house", "https://sisu.house/about"),
    ("archive-spa-haesurang", "spa-haesurang-busan", "Spa Haesurang, floor by floor", "Spa Haesurang", "Hand drawing stacking the floors of Spa Haesurang (2F lobby, 3–4F powder rooms, 4–5F locker rooms, 5F jjimjil room, 7F open-air bath). No official info for 6F.", "spahaesurang.co.kr", "https://spahaesurang.co.kr/amenities"),
    ("archive-heosimcheong", "heosimcheong-busan", "Hurshimchung, floor by floor", "Hurshimchung (Hotel Nongshim)", "Hand drawing stacking the floors of Hotel Nongshim Hurshimchung (B1·B2 parking, 1F restaurant, 2F banquet, 3F jjimjil room, 4F lockers and front desk, 5F hot-spring baths).", "hotelnongshim.com", "https://www.hotelnongshim.com/kr/?pCode=Hurshimchung"),
    ("archive-termeden", "termeden-icheon", "Termeden, zone by zone", "Termeden", "Hand drawing of Termeden’s zones (indoor pool & spa, hot-spring bathhouse, jjimjil spa, outdoor pool & spa) on one sheet.", "termeden.com", "https://termeden.com/poolspa_explain.html"),
    ("archive-orakai-cheonggyesan", "orakai-cheonggyesan", "Orakai Cheonggyesan, floor by floor", "Orakai Cheonggyesan", "Hand drawing stacking the B1 sauna (dry and wet saunas, several pools, walk-in body scrub) and the B2 EV charging area of Orakai Cheonggyesan Hotel. The sauna’s interior layout is not drawn.", "cgs.orakaihotels.com", "https://cgs.orakaihotels.com/kr/about/facilities_sauna.asp"),
    ("archive-crossfit-super-sapiens", "crossfit-super-sapiens", "CrossFit Super Sapiens, floor by floor", "CrossFit Super Sapiens", "Hand drawing stacking the floors of CrossFit Super Sapiens (1F main training floor, 2F showers, lockers and accessory area, 3F tanning and staff space).", "crossfitsupersapiens.com", "https://crossfitsupersapiens.com/our-facility"),
]
ARCHIVE_BY_VENUE = {a[1]: a for a in ARCHIVE}
# 조감도 손그림 (위에서 내려다본 평면). illustrations/make_aerial.py
AERIAL = [
    ('aerial-termeden', 'termeden-icheon', "Hand-drawn bird's-eye plan of Termeden in Icheon: outdoor infinity pool, forest bath, mountain pool and terme tubs; indoor pool and spa, hot-spring bathhouse with cold bath and saunas, and the jjimjil spa; day pass ₩49,000 (adult, November)."),
    ('aerial-spa-haesurang', 'spa-haesurang-busan', "Hand-drawn bird's-eye plan of Spa Haesurang's sauna floor in Busan: dry and wet saunas, warm, hot and cold pools, event bath, rows of showers (about 80) and powder room; sauna ₩15,000 on weekdays."),
    ('aerial-heosimcheong', 'heosimcheong-busan', "Hand-drawn bird's-eye plan of the 5th-floor hot-spring baths at Hurshimchung, Busan: longevity, event, celadon, cave, cold, half-body, salt and open-air baths, mini pool, steam and dry saunas, showers and scrub corner; ₩15,000 on weekdays."),
    ('aerial-orakai-cheonggyesan', 'orakai-cheonggyesan', "Hand-drawn bird's-eye plan of the basement sauna at Orakai Cheonggyesan Hotel, Seoul: dry and wet saunas, several pools, showers and a walk-in body scrub; adult ₩24,000."),
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", action="store_true"); ap.add_argument("--ads"); ap.add_argument("--today"); ap.add_argument("--out", default=str(DIST))
    ap.add_argument("--verified-examples", action="store_true", help="preview only: render fake data from data/verified/examples")
    a = ap.parse_args()
    today = ymd(a.today) if a.today else dt.datetime.now(ZoneInfo("Asia/Seoul")).date()
    global CLOSED; CLOSED = active_closed(today)
    dist = Path(a.out)
    venues, events, skipped, ev_skipped = load(today, a.ads)
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
    shutil.copy(ROOT / "static/favicon.svg", dist / "favicon.svg")
    for _f in (ROOT / "static/root").glob("*"):  # 루트 정적 파일(구글 서치콘솔 소유확인 등; 지우지 말 것)
        if _f.is_file(): shutil.copy(_f, dist / _f.name)
    (dist / "events.ics").write_text(ics(events), encoding="utf-8")
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
    if bad:
        for b in bad: print("LEAK-CHECK FAIL:", b)
        sys.exit(2)
    print("leak check OK (no owner credentials / third-party-source names / trackers)")

if __name__ == "__main__":
    main()
