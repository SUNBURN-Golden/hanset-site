#!/usr/bin/env python3
"""HANSET / 한셋 static site builder.

Reads ../boxes.csv ../saunas.csv ../events.csv (single source of truth for numbers, dates, URLs) and
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
PENDING = "Pending"
CITY_KEY = {"Seoul": "seoul", "Busan": "busan", "Jeju": "jeju", "Gyeonggi": "gyeonggi"}
WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SAFETY = "Cardiovascular disease, pregnancy, after drinking, or feeling unwell? Skip the sauna and cold plunge, or ask a doctor first. This is not medical advice."
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
FCHK_KEYS = [("tattoo", "Tattoos"), ("card", "Foreign card payment"), ("english", "English booking / staff"), ("dropin", "Drop-in for visitors")]
def fchk(v):
    d = FOREIGNER.get(v["slug"], {})
    rows = "".join(f'<tr><th>{lab}</th>' + (f'<td>{E(d[k])}</td>' if d.get(k) else '<td class="unk">Not confirmed</td>') + '</tr>' for k, lab in FCHK_KEYS)
    return f'<h2>Foreigner checklist</h2><table class="facts"><tbody>{rows}</tbody></table><p class="muted">From the official page only, checked {dot(v["last_verified"])}. Not confirmed? Ask the venue before you go.</p>'

GUIDES = [  # (slug, title, description); page bodies are built in guides()
    ("tattoo-friendly-saunas-korea", "Tattoos and saunas in Korea", "What official pages of Korean hotel saunas and spas say about tattoos. Verified, dated, sourced."),
    ("hotel-saunas-korea-non-guests", "Hotel saunas in Korea: can non-guests get in?", "Which Korean hotel saunas are open to visitors without a room, per their official pages."),
    ("crossfit-hyrox-drop-in-korea", "CrossFit & HYROX drop-in in Korea", "CrossFit and HYROX boxes in Korea that list drop-ins or English support on their official pages.")]

def guides(site, venues):
    sau = [v for v in venues if v["kind_key"] == "sauna"]; box = [v for v in venues if v["kind_key"] == "box"]
    def tbl(vs, key, head):
        r = "".join(f'<tr><th><a href="/finder/{v["slug"]}/">{E(v["title"])}</a><br><span class="muted">{E(v["city_ko"])}</span></th>' + (f'<td>{E(FOREIGNER.get(v["slug"],{}).get(key))}</td>' if FOREIGNER.get(v["slug"],{}).get(key) else '<td class="unk">Not confirmed</td>') + f'<td>{dot(v["last_verified"])} · <a href="{E(v["source"])}" rel="noopener noreferrer">{E(host(v["source"]))}</a></td></tr>' for v in vs)
        return f'<table class="facts"><thead><tr><th>Venue</th><th>{head}</th><th>Checked · Source</th></tr></thead><tbody>{r}</tbody></table>'
    note = '<p class="muted">Official pages only. Every line links to its source and check date. Rules change. Confirm with the venue before you go.</p>'
    bodies = {"tattoo-friendly-saunas-korea": "<h1>Tattoos? Read this first.</h1><p class=\"lead\">Some Korean saunas limit entry with tattoos. Here’s exactly what each official page says. Nothing more.</p>" + note + tbl(sau, "tattoo", "Tattoo rule"),
              "hotel-saunas-korea-non-guests": "<h1>No room? Some doors still open.</h1><p class=\"lead\">Hotel saunas in Seoul, Busan and Jeju. Who gets in, straight from the official pages.</p>" + note + tbl(sau, "dropin", "Open to visitors?") + f'<p class="safety">{SAFETY}</p>',
              "crossfit-hyrox-drop-in-korea": "<h1>Traveling? Keep training.</h1><p class=\"lead\">CrossFit and HYROX boxes in Korea. Drop-in and English support, from official pages only.</p>" + note + tbl(box, "dropin", "Drop-in") + "<h2>English support</h2>" + tbl(box, "english", "English")}
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
            f'<p class="muted">Official pages only. Dates change. Check before you go.</p></section>')

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
    else: b.append(badge("Verified" if v["status"] == "verified" else "Partly verified", "badge-ok" if v["status"] == "verified" else "badge-part"))
    if v["stale"]: b.append(badge("Outdated", "badge-old"))
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
SUN_TXT = {"yes": "Open Sunday (verified)", "no": "Closed Sunday (verified)", "unk": "Sunday: pending"}

def cav(v): return f'<div class="meta cav">※ {E(v["price_caveat"])}</div>' if v["price_caveat"] and v["price"] else ""

def venue_card(v):
    price = (f'<div class="price">{E(v["price_label"])} {E(v["price"])}</div>' if v["price"]
             else '<div class="price unk">Price pending</div>')
    attrs = (f'data-city="{v["city_key"]}" data-type="{v["kind_key"]}" data-sunday="{v["sunday"]}" data-price="{1 if v["price"] else 0}" data-cf="{1 if v["off"]["cf"] else 0}" data-hyrox="{1 if v["off"]["hyrox"] else 0}" data-sub="{v["sub"]}" data-order="{v["order"]}" data-date="{v["last_verified"].isoformat()}" data-title="{E(v["title"])}" '
             f'data-name="{E((v["title"] + " " + v["name"] + " " + v["location"] + " " + v["title_ko"] + " " + v["location_ko"]).lower())}"')
    return (f'<article class="card" {attrs}>{v_badges(v)}<h3><a href="/finder/{v["slug"]}/">{E(v["title"])}</a></h3>'
            f'<div class="loc">{E(v["location"])}</div>{price}{cav(v)}<div class="loc">{SUN_TXT[v["sunday"]]}</div>{off_html(v)}'
            f'<div class="meta">Checked {dot(v["last_verified"])} · Source {E(host(v["source"]))}</div></article>')

def mini_card(v):
    price = (f'{E(v["price_label"])} {E(v["price"])}' if v["price"] else "Price pending")
    return (f'<article class="card">{v_badges(v)}<h3><a href="/finder/{v["slug"]}/">{E(v["title"])}</a></h3>'
            f'<div class="loc">{E(v["location"])}</div><div class="price{"" if v["price"] else " unk"}">{price}</div>{off_html(v)}'
            f'<div class="meta">Checked {dot(v["last_verified"])}</div></article>')

def home_card(v):
    price = (f'{E(v["price_label"])} {E(v["price"])}' if v["price"] else "Price pending")
    area = v["area"] or v["location"].split(" · ", 1)[0]
    lv = v["last_verified"]
    return (f'<article class="card home-card"><h3><a href="/finder/{v["slug"]}/">{E(v["title"])}</a></h3>'
            f'<div class="home-info"><span>{E(area)}</span> <span aria-hidden="true">·</span> '
            f'<span>{E(v["kind"])}</span> <span aria-hidden="true">·</span> '
            f'<span class="price{"" if v["price"] else " unk"}">{price}</span></div>'
            f'<div class="home-date">Checked {MON[lv.month]} {lv.day}</div></article>')

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
    # ---- home
    picks = [v for v in venues if v["pick"]]
    preview = picks[:2]
    if len(preview) < 2:
        rest = [v for v in venues if v["status"] == "verified" and v not in preview]
        preview = preview + rest[:2 - len(preview)]
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
<p class="lead">CrossFit and HYROX drop-ins, boutique saunas and hotel saunas across Korea. Tattoo rules, non-guest access and drop-in, from each official page.</p>
<div class="hero-ctas"><a class="btn btn-d" href="/finder/">Find a place</a> <a class="btn btn-ghost" href="/guides/">Visitors guide</a></div></section>

<form class="find-panel" action="/finder/" method="get" role="search">
<div class="find-field"><label for="home-q">Name or area</label><input id="home-q" name="q" type="search" placeholder="e.g. Hannam, Busan, hotel"></div>
<div class="find-field"><label for="home-city">City</label><select id="home-city" name="city"><option value="">Any city</option><option value="seoul">Seoul</option><option value="busan">Busan</option><option value="jeju">Jeju</option><option value="gyeonggi">Gyeonggi</option></select></div>
<div class="find-field"><label for="home-type">Type</label><select id="home-type" name="type"><option value="">Any kind</option><option value="box">CrossFit · HYROX boxes</option><option value="sauna">Saunas</option></select></div>
<button class="btn btn-d" type="submit">Search</button></form>
<p class="find-note">First time in Korea? <a href="/guides/">Read the Visitors guide →</a></p>

<section class="sec home-results"><div class="sec-h"><h2>Only verified places.</h2><a href="/finder/">See all {len(venues)} →</a></div>
<div class="grid home-grid">{"".join(home_card(v) for v in preview)}</div>
<p class="muted trust">Every source. Every date.</p></section>

<section class="sec home-guides"><h2>Visitors guide</h2>
<ul class="guide-list">{guide_rows}</ul></section>

{race_strip}
<p class="safety-line">{SAFETY}</p>

{home_mag}

<section class="sec"><div class="sec-h"><h2>First time? No problem.</h2><a href="/guide/#pictures">View larger</a></div>
<p class="muted">CrossFit boxes, Boutique Saunas · Hotel Saunas. One drawing each, before you go. General examples. May differ from the actual venue.</p>
<div class="illrow">{"".join(f'<a class="illc" href="/guide/#{k}"><picture><source media="(min-width:720px)" srcset="/assets/img/guide/{f}.svg"><img src="/assets/img/guide/{f}-thumb.png" width="252" height="315" loading="lazy" decoding="async" alt="{E(d)}"></picture><span><b>{E(t)}</b><em>View ›</em></span></a>' for k, f, t, d in ILLUSTRATIONS)}</div></section>

<div class="slot" data-slot="kix-ticketing" hidden></div>
<section class="sec"><div class="sec-h"><h2>See it from above.</h2><a href="/archive/">Archive ›</a></div>
<p class="muted">Floors and zones from official guides, redrawn by hand. Layouts are conceptual. May differ from the actual venue.</p>
<div class="illrow arcrow">{"".join(f'<a class="illc" href="/archive/#{vs}"><picture><source media="(min-width:720px)" srcset="/assets/img/archive/{f}-mid.png"><img src="/assets/img/archive/{f}-thumb.png" width="252" height="315" loading="lazy" decoding="async" alt="{E(d)}"></picture><span><b>{E(st)}</b><em>View ›</em></span></a>' for f, vs, t, st, d, sl, su in ARCHIVE)}</div>
<p><a class="btn btn-d" href="/archive/">See every drawing ›</a></p></section>'''
    site.add("/", f"{BRAND_EN} — CrossFit, HYROX & Sauna Wellness Magazine in Korea", "CrossFit, HYROX, hotel and boutique saunas in Korea. A drop-in and sauna finder plus race dates. Every entry with an official source and check date.", body, "home", ("dday.js",))

    # ---- finder
    ordered = sorted(venues, key=lambda v: (not v["ad"], v["status"] != "verified", v["order"]))
    cnt_box = sum(v["kind_key"] == "box" for v in venues); cnt_sauna = len(venues) - cnt_box
    seg = lambda g, items: '<div class="seg" data-seg="%s">' % g + "".join(f'<button type="button" data-v="{k}" aria-pressed="false">{E(t)}</button>' for k, t in items) + "</div>"
    fbody = f'''
<div class="finder-head"><h1>{FINDER_LABEL}</h1>
<p class="lead">Drop-in boxes. Hotel and boutique saunas. Large spas and hot springs. {len(venues)} officially verified places ({cnt_box} boxes · {cnt_sauna} saunas), and counting.</p>
<details class="howto"><summary>How the filters work</summary><ul>
<li><strong>City · Type</strong>: pick where, then box or sauna. Saunas split further into hotel, boutique, and large spa · hot spring.</li>
<li><strong>Sunday</strong>: "open/closed (verified)" only when the official page says so. Otherwise, "pending."</li>
<li><strong>Price confirmed</strong>: keeps only places whose price we read on the official page.</li>
<li><strong>Official lists</strong>: keeps only places we saw ourselves in CrossFit’s or HYROX’s official list on the check date. No mark means still checking.</li>
<li><strong>Sort</strong>: default, recently checked, or A–Z.</li>
</ul><p class="muted">More in the <a href="/guide/">Guide</a> and <a href="/faq/">FAQ</a>.</p></details></div>
<form class="filters" aria-label="Filters">
 <div class="fgroup"><label for="f-q">Search name or area</label><input id="f-q" type="search" placeholder="e.g. Hannam, Busan, hotel" autocomplete="off"></div>
 <div class="fgroup"><span>City</span>{seg("city", [("", "All"), ("seoul", "Seoul"), ("busan", "Busan"), ("jeju", "Jeju"), ("gyeonggi", "Gyeonggi")])}</div>
 <div class="fgroup"><span>Type</span>{seg("type", [("", "All"), ("box", "CrossFit · HYROX boxes"), ("sauna", "Saunas")])}</div>
 <div class="fgroup"><label for="f-sub">Sauna type</label><select id="f-sub"><option value="">All</option><option value="hotel">Hotel Saunas</option><option value="boutique">Boutique Saunas</option><option value="spa">Large Spa · Hot Spring</option></select></div>
 <div class="fgroup"><label for="f-sort">Sort</label><select id="f-sort"><option value="">Default</option><option value="date">Recently checked</option><option value="name">A–Z</option></select></div>
 <div class="fgroup"><label for="f-sunday">Sunday</label><select id="f-sunday"><option value="">All</option><option value="yes">Open (verified)</option><option value="no">Closed (verified)</option><option value="unk">Pending</option></select></div>
 <div class="fgroup"><span>Price</span><label class="chk"><input id="f-price" type="checkbox"> Price confirmed only</label></div>
 <div class="fgroup"><span>Official lists</span><label class="chk"><input id="f-hyrox" type="checkbox"> HYROX gyms (official list)</label> <label class="chk"><input id="f-cf" type="checkbox"> CrossFit affiliates (verified)</label></div>
 <div class="fgroup"><span>&nbsp;</span><button id="f-reset" data-reset type="button" class="btn btn-d">Start over</button></div>
</form>
<p class="count" id="f-count" aria-live="polite">{len(venues)} places</p>
<div class="grid" id="finder-list">{"".join(venue_card(v) for v in ordered)}</div>
<div class="empty" id="f-empty" hidden><p>Nothing here yet.</p><p class="muted">Try fewer filters. More verified places are on the way.</p><button type="button" class="btn btn-d" data-reset>Start over</button></div>
<div class="note">"Verified" means we read the info directly on the venue’s official page. "Partly verified" means only some items, like the address. Sunday is filled in only when the official page says so. "CrossFit affiliate verified" and "HYROX official gym list verified" appear only where we saw the place ourselves in that official page or list on the check date. No mark means still checking.</div>
<noscript><p class="muted">Filters need JavaScript. The full list still works without it.</p></noscript>'''
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
                cell = f'<td class="unk">{PENDING}</td>' if not val.strip() else f'<td>{E(val)}</td>'
                out.append(f'<tr><th>{E(k)}</th>{cell}</tr>')
            return "".join(out)
        links = []
        if v["website"] and "map.naver.com" not in v["website"]:
            links.append(f'<a class="btn btn-d" href="{E(v["website"])}" rel="{"sponsored " if v["ad"] else ""}noopener noreferrer">Official site</a>')
        if v["booking"] and v["booking"] != v["website"]:
            links.append(f'<a class="btn btn-d" href="{E(v["booking"])}" rel="noopener noreferrer">Book</a>')
        q = quote(v["title_ko"] + (" " + v["address"] if v["address"] else ""))
        links.append(f'<a class="btn btn-d" href="https://map.naver.com/p/search/{q}" rel="noopener noreferrer">Open in map</a>')
        srcs = [f'<a href="{E(v["source"])}" rel="noopener noreferrer">{E(host(v["source"]))}</a>'] + [f'<a href="{E(u)}" rel="noopener noreferrer">{E(l)}</a>' for l, u in v["extra_sources"]]
        adnote = (f'<div class="note ad"><strong>Ad</strong> · This entry is labeled as an ad. Verified info still comes from official sources. '
                  f'{E(v["ad_note"])}</div>' if v["ad"] else "")
        tips = ("<h2>Tips · Heads-up</h2><ul class='clean'>" + "".join(f"<li>{E(t)}</li>" for t in v["tips"]) + "</ul>") if v["tips"] else ""
        unv = ("<h2>Not yet verified</h2><ul class='clean'>" + "".join(f"<li>{E(t)}</li>" for t in v["unverified"]) + "</ul>") if v["unverified"] else ""
        stale = (f'<div class="note">Last checked over {STALE_DAYS} days ago. Confirm with the venue before you go.</div>' if v["stale"] else "")
        safety = f'<p class="safety">{SAFETY}</p>' if v["kind_key"] == "sauna" else ""
        offrows = "".join(f'<tr><th>Official list</th><td>{E(t)}</td></tr>' for t in off_marks(v))
        extra_addr = f'<tr><th>Address</th><td><span lang="ko">{E(v["address"])}</span></td></tr>' if v["address"] and not any(k == "주소" for k, _ in v["facts_ko"]) else ""
        sunrow = f'<tr><th>Sunday</th><td{"" if v["sunday"] != "unk" else " class=unk"}>{SUN_TXT[v["sunday"]] if v["sunday"] != "unk" else PENDING}</td></tr>' if v["kind_key"] == "box" or v["sunday"] != "unk" else ""
        if v["kind_key"] == "box" and any("일요일" in k or "휴무" in k for k, _ in v["facts_ko"]) and v["sunday"] != "unk": sunrow = ""
        if v["kind_key"] == "box" and v["sunday"] == "unk" and any("일요일" in k for k, _ in v["facts_ko"]): sunrow = ""
        _ar = ARCHIVE_BY_VENUE.get(v["slug"])
        arch_blk = (f'<section class="archbox"><h2>Hand-drawn sketch</h2><a class="illc" href="/archive/#{v["slug"]}"><picture><img src="/assets/img/archive/{_ar[0]}-thumb.png" width="252" height="315" loading="lazy" decoding="async" alt="{E(_ar[4])}"></picture>'
                    f'<span><b>{E(_ar[2])}</b><em>Hand-drawn from the official guide · Tap to view ›</em></span></a></section>') if _ar else ""
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
<p class="lead">{E(v["summary"])}</p>
<table class="facts"><tbody>{rows()}{extra_addr}{sunrow}{offrows}</tbody></table>
{tips}{unv}{fchk(v)}{arch_blk}
<div class="verify"><p><strong>Last checked {dot(v["last_verified"])}</strong></p><p>Source: {" · ".join(srcs)}</p>
<p>Something's off? Tell us via <a href="/corrections/">Corrections</a>. Prices and closing days change.</p></div>
{next_html}
<div class="btnrow">{"".join(links)}</div>{safety}</div>'''
        site.add(f"/finder/{v['slug']}/", f"{v['title']}", clip(f"{v['title']} — {v['summary']}"), body, "finder")

    # ---- events
    cards = "".join(event_card(e, today) for e in upcoming)
    if not upcoming: cards = '<div class="empty"><p>No officially confirmed races right now.</p><p class="muted">We add them as they’re confirmed.</p></div>'
    ebody = f'''<div class="page"><h1>Event Calendar</h1>
<p class="lead">Official dates only. Straight from the organizers.</p></div>
<div class="ev">{cards}</div>
{vevents_section(vevents)}
<div class="note">Races listed only on aggregator sites wait until the organizer confirms. Fees appear only when read on the official page. <a href="/events.ics">Download all dates (.ics)</a></div>'''
    site.add("/events/", "Event Calendar", "HYROX Seoul, Urban Wave, the CrossFit Open and more. Race dates confirmed on official pages.", ebody, "events", ("dday.js",))

    # ---- creators (verified feed; page only when there is data)
    if creators:
        cbody = (f'<div class="page"><h1>Creators</h1><p class="lead">People worth following. Training, racing, sauna.</p></div>'
                 f'<section class="sec vsec" aria-label="Creators"><div class="vlist">{"".join(creator_card(c) for c in creators)}</div>'
                 f'<p class="muted">Public profiles only. Each one checked on the date shown.</p></section>')
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
             f'<div class="page"><h1>Magazine</h1><p class="lead">Research, made readable. Every article cited.</p><p class="muted">Articles are currently in Korean.</p></div><div class="grid">{mcards}</div>', "magazine")

    # ---- archive (손그림 아카이브)
    items = "".join(
        f'<article class="arc" id="{vs}"><a class="arcimg" href="/assets/img/archive/{f}.svg" target="_blank" rel="noopener" aria-label="{E(t)}: view larger (new tab)">'
        f'<picture><source media="(min-width:720px)" srcset="/assets/img/archive/{f}-mid.png"><img src="/assets/img/archive/{f}-thumb.png" width="252" height="315" loading="lazy" decoding="async" alt="{E(d)}"></picture></a>'
        f'<div class="arcbody"><h2>{E(t)}</h2><p class="meta">Conceptual layout · Hand-drawn from the official guide · May differ from the actual venue</p>'
        f'<p class="meta">Source <a href="{E(su)}" rel="noopener noreferrer">{E(sl)}</a> · Checked {ARCHIVE_DATE}</p>'
        f'<div class="btnrow"><a class="btn btn-d" href="/assets/img/archive/{f}.svg" target="_blank" rel="noopener">View larger ↗</a><a class="btn btn-d" href="/finder/{vs}/">Venue details</a></div></div></article>'
        for f, vs, t, st, d, sl, su in ARCHIVE)
    abody = f'''<div class="page"><h1>Venue Sketches</h1>
<p class="lead">Floors and zones from official guides. Redrawn by hand, from above (isometric).</p>
<ul><li>No photos or official maps copied or traced. Nothing drawn that the official guide doesn’t describe.</li>
<li>Layouts are conceptual. Only floor and zone names and order follow the official guide. Position and size may differ from the actual venue.</li>
<li>Labels in the drawings are in Korean. Tap a drawing to open it larger in a new tab (pinch to zoom).</li></ul></div>
<div class="arcgrid">{items}</div>
<div class="page"><p class="muted">More drawings as official floor and zone guides are published in text. {SAFETY}</p></div>'''
    site.add("/archive/", "Venue Sketches", "Hand-drawn sketches of floors and zones, based on official venue guides (conceptual layouts). May differ from the actual venue. Every drawing with a source and check date.", abody, "guide")

    # ---- static pages
    for slug, title, desc, active in [("guide", "Guide", "How to read HANSET's finder, badges and check dates, and what to know before you go.", "guide"),
                                      ("faq", "FAQ", "How HANSET verifies, what 'Pending' means, how to request a correction, and more.", "guide"),
                                      ("about", "About", "HANSET is a wellness magazine that lists CrossFit, HYROX, hotel and boutique saunas in Korea with official sources and check dates.", "about"),
                                       ("privacy", "Privacy", "HANSET runs without trackers.", "about"),
                                       ("corrections", "Corrections", "See something wrong? Tell us.", "about")]:
        frag = (ROOT / f"content/pages/{slug}.html").read_text(encoding="utf-8")
        if slug == "guide":
            figs = "".join(f'<figure class="fig" id="{k}"><a class="figlink" href="/assets/img/guide/{f}.svg" target="_blank" rel="noopener" aria-label="{E(t)}: view larger (new tab)"><img src="/assets/img/guide/{f}.svg" width="1080" height="1350" loading="lazy" decoding="async" alt="{E(d)}"></a><figcaption><strong>{E(t)}</strong><br>A general illustration. May differ from the actual venue. Labels are in Korean. <a href="/assets/img/guide/{f}.svg" target="_blank" rel="noopener">View larger ↗</a></figcaption></figure>' for k, f, t, d in ILLUSTRATIONS)
            frag = frag.replace("<!--ILLUSTRATIONS-->", f'<h2 id="pictures">In pictures</h2><p>Hand-drawn guides for first-timers. General examples. Layout and process may differ from the actual venue. Tap a drawing to open it larger in a new tab (pinch to zoom).</p><div class="figs">{figs}</div>')
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
    ("box", "crossfit-box-plan", "Inside a CrossFit box", "Hand-drawn layout sketch of a typical CrossFit box: entrance and front desk, lockers and changing area, whiteboard, WOD area, rig, open gym and barbell racks, rowing and bike zone. A general example. May differ from the actual venue."),
    ("dropin", "dropin-steps", "How a drop-in works", "Hand-drawn CrossFit drop-in steps: 1 check timetable and price on the official page, 2 book or ask, 3 check in on arrival, 4 join the class, 5 clean up and pay. Class format and price vary by box."),
    ("sauna", "sauna-guide", "How to sauna", "Hand-drawn sauna sequence and a typical layout of dry and wet saunas, showers, cold plunge and rest area. With cardiovascular disease, during pregnancy, after drinking, or when you feel unwell, avoid saunas and cold plunges or consult a doctor. Facilities vary by venue."),
]

def ics(events):
    def esc(s): return s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//HANSET//events//EN", "CALSCALE:GREGORIAN", "X-WR-CALNAME:HANSET Events"]
    for e in events:
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
    ("archive-sisu-house", "sisu-house", "Sisu House, floor by floor", "Sisu House", "Isometric hand drawing stacking the floors from Sisu House’s official space guide (1F Welcoming Zone, 1.5F Touch Zone, 2-3F Private Zone) and its ritual sequence. Conceptual layout.", "sisu.house", "https://sisu.house/about"),
    ("archive-spa-haesurang", "spa-haesurang-busan", "Spa Haesurang, floor by floor", "Spa Haesurang", "Isometric hand drawing stacking the floors from Spa Haesurang’s official amenities guide (2F lobby, 3–4F powder rooms, 4–5F locker rooms, 5F jjimjil room, 7F open-air bath). No official info for 6F. Conceptual layout.", "spahaesurang.co.kr", "https://spahaesurang.co.kr/amenities"),
    ("archive-heosimcheong", "heosimcheong-busan", "Hurshimchung, floor by floor", "Hurshimchung (Hotel Nongshim)", "Isometric hand drawing stacking Hotel Nongshim Hurshimchung’s official floor guide (B1·B2 parking, 1F restaurant, 2F banquet, 3F jjimjil room, 4F lockers and front desk, 5F hot-spring baths). Conceptual layout.", "hotelnongshim.com", "https://www.hotelnongshim.com/kr/?pCode=Hurshimchung"),
    ("archive-termeden", "termeden-icheon", "Termeden, zone by zone", "Termeden", "Isometric hand drawing gathering the zones from Termeden’s official facility guide (indoor pool & spa, hot-spring bathhouse, jjimjil spa, outdoor pool & spa) on one sheet. Positions are not the real layout.", "termeden.com", "https://termeden.com/poolspa_explain.html"),
    ("archive-orakai-cheonggyesan", "orakai-cheonggyesan", "Orakai Cheonggyesan, floor by floor", "Orakai Cheonggyesan", "Isometric hand drawing stacking the B1 sauna (dry and wet saunas, several pools, walk-in body scrub) and the B2 EV charging area from Orakai Cheonggyesan Hotel’s official guide. The sauna’s interior layout is not drawn.", "cgs.orakaihotels.com", "https://cgs.orakaihotels.com/kr/about/facilities_sauna.asp"),
    ("archive-crossfit-super-sapiens", "crossfit-super-sapiens", "Super Sapiens, floor by floor", "CrossFit Super Sapiens", "Isometric hand drawing stacking the floors from CrossFit Super Sapiens’ official facility guide (1F main training floor, 2F showers, lockers and accessory area, 3F tanning and staff space). Conceptual layout within floors.", "crossfitsupersapiens.com", "https://crossfitsupersapiens.com/our-facility"),
]
ARCHIVE_BY_VENUE = {a[1]: a for a in ARCHIVE}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", action="store_true"); ap.add_argument("--ads"); ap.add_argument("--today"); ap.add_argument("--out", default=str(DIST))
    ap.add_argument("--verified-examples", action="store_true", help="preview only: render fake data from data/verified/examples")
    a = ap.parse_args()
    today = ymd(a.today) if a.today else dt.datetime.now(ZoneInfo("Asia/Seoul")).date()
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
