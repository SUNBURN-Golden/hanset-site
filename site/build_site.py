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

ROOT = Path(__file__).resolve().parent
DATA = ROOT.parent
DIST = ROOT / "dist"
SITE_URL = "https://hanset.soulbound.co.kr"
BRAND_KO, BRAND_EN = "한셋", "HANSET"
FINDER_LABEL = "Drop-in & Sauna Finder"
CITY_KO = {"Seoul": "서울", "Busan": "부산", "Jeju": "제주", "Gyeonggi": "경기"}
CITY_KEY = {"Seoul": "seoul", "Busan": "busan", "Jeju": "jeju", "Gyeonggi": "gyeonggi"}
WD = "월화수목금토일"
SAFETY = "심혈관 질환·임신·음주 후·컨디션 불량 시 사우나·냉수욕을 피하거나 의사와 상담하세요. 이 콘텐츠는 의학적 조언이 아닙니다."
STALE_DAYS = 90
FORBIDDEN = [r"\b442\b", r"KWANG", r"크왕", r"우승", r"로스쿨", r"법무", r"법학", r"변호사", r"\bMZ\b", r"saunaday", r"사우나데이",
             r"moduisauna", r"crossfitlee", r"LUWEI", r"한세트", r"땀살롱", r"PRIVATE-DM"]

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
        return (f"{d.month}/{d.day}({wd(d)})" if d.year == ref_year else f"{d.year}.{d.month}.{d.day}({wd(d)})")
    return f(a) if a == b else f"{f(a)}–{f(b)}"

# ------------------------------------------------------------------ data
def load(today, ads_path):
    content = json.loads((ROOT / "content/venues.json").read_text(encoding="utf-8"))
    ev_content = json.loads((ROOT / "content/events.json").read_text(encoding="utf-8"))
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
            extra = [("크로스핏 공식 어필리에이트 목록", r["cf_listing_url"])] if r["cf_listing_url"] and r["cf_listing_url"] != src else []
            addr = r["address"]
            kind_key = "box"; sunday = c.get("sunday") or {"yes": "yes", "no": "no"}.get(r["sunday_open"], "unk")
            price = won(r["drop_in_price_krw"]); site = r["website"] or ""; book = r["booking_url"] or ""
            chk = r.get("official_check_date", "")
            cf_ok = r.get("cf_official_check") == "yes" and bool(r.get("cf_official_url")); hy_ok = r.get("hyrox_official_check") == "yes" and bool(r.get("hyrox_official_url"))
            off = dict(date=chk, cf=r.get("cf_official_url", "") if cf_ok else "", hyrox=r.get("hyrox_official_url", "") if hy_ok else "")
            if cf_ok: extra = [("크로스핏 공식 어필리에이트 페이지", off["cf"])]
            if hy_ok: extra = extra + [("하이록스 공식 지점 목록", off["hyrox"])]
        else:
            if not conf.startswith("high"): sys.exit(f"ERROR: refusing to publish sauna {name!r} with confidence {conf!r}")
            status = c["status"]
            src = r["source_url"]; extra = []; addr = ""
            kind_key = "sauna"; sunday = c.get("sunday") or "unk"; off = dict(date="", cf="", hyrox="")
            price = won(r["price_krw"]); site = r["website"] or ""; book = ""
        ptxt = price or "확인 중"
        def fill(s): return s.replace("{price}", ptxt)
        facts = [[k, fill(v)] for k, v in c["facts"]]
        lv = ymd(r["last_verified"])
        a = ads.get(c["slug"], {})
        v = dict(c=c, name=name, slug=c["slug"], kind_key=kind_key, kind=c["kind"], status=status, title=c["title"],
                 city=r["city"], city_ko=CITY_KO.get(r["city"], r["city"]), city_key=CITY_KEY.get(r["city"], r["city"].lower()),
                 location=c["location"], summary=c["summary"], facts=facts, tips=c.get("tips", []), unverified=[fill(x) for x in c.get("unverified", [])],
                 tags=[t for t in c.get("tags", []) if t != "일부 확인"], price=price, price_label=c.get("price_label", "요금"),
                 sunday=sunday, last_verified=lv, stale=(today - lv).days > STALE_DAYS, source=src, extra_sources=extra,
                 website=site, booking=book, address=addr, hyrox=bool(c.get("hyrox")), pick=bool(c.get("pick")),
                 price_caveat=c.get("price_caveat", ""), sub=("box" if kind_key == "box" else (c.get("sub") or ("hotel" if "호텔" in c["kind"] else "boutique"))), ad=bool(a.get("ad", c.get("ad", False))), ad_note=a.get("note", ""), flags=c.get("flags", {}), order=len(venues), off=off)
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
        events.append(dict(c=c, name=name, slug=c["slug"], title=c["title"], kind=c["kind"], start=s, end=e, city=c["city"], venue=c["venue"],
                           facts=c["facts"], note=c.get("note", ""), url=c["official_url"], source_label=c["source_label"],
                           last_verified=ymd(r["last_verified"]), feature=bool(c.get("feature"))))
    events.sort(key=lambda x: x["start"])
    ev_skipped = [r["name"] for r in read_csv("events.csv") if r["name"] not in ev_content]
    return venues, events, skipped, ev_skipped

# ------------------------------------------------------------------ html pieces
class Site:
    def __init__(self, today, release):
        self.today, self.release = today, release
        self.pages = {}  # path -> html

    def head(self, title, desc, path):
        full = title if title.startswith(BRAND_KO) else f"{title} · {BRAND_KO} {BRAND_EN}"
        robots = "" if self.release else '<meta name="robots" content="noindex,nofollow">\n'
        canon = f'<link rel="canonical" href="{SITE_URL}{path}">\n' if self.release else ""
        og = (f'<meta property="og:type" content="website"><meta property="og:site_name" content="{BRAND_KO} {BRAND_EN}">'
              f'<meta property="og:title" content="{E(full)}"><meta property="og:description" content="{E(desc)}">'
              + (f'<meta property="og:url" content="{SITE_URL}{path}"><meta property="og:locale" content="ko_KR">'
                 f'<meta property="og:image" content="{SITE_URL}/assets/img/og-image.png"><meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">'
                 f'<meta property="og:image:alt" content="한셋 HANSET - 손그림 크로스핏 박스 배치 안내 그림">'
                 f'<meta name="twitter:card" content="summary_large_image">' if self.release else ""))
        return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<title>{E(full)}</title><meta name="description" content="{E(desc)}">\n{robots}{canon}{og}'
                f'<meta name="theme-color" content="#F5F2EC"><link rel="icon" href="/favicon.svg" type="image/svg+xml">'
                f'<link rel="preload" href="/assets/fonts/Pretendard-Regular.subset.woff2" as="font" type="font/woff2" crossorigin>'
                f'<link rel="stylesheet" href="/assets/css/site.css?v={asset_ver("css/site.css")}"></head>')

    def nav(self, active):
        def cur(k): return ' aria-current="page"' if active == k else ""
        return (f'<a class="skip" href="#main">본문으로 건너뛰기</a><header class="site-header"><div class="wrap">'
                f'<div class="hrow"><a class="brand" href="/"><i></i>{BRAND_KO}<small>{BRAND_EN}</small></a>'
                f'<a class="pin" href="/finder/"{cur("finder")}>{FINDER_LABEL}</a></div>'
                f'<nav class="nav" aria-label="주 메뉴"><a href="/"{cur("home")}>홈</a>'
                f'<a href="/magazine/"{cur("magazine")}>매거진</a><a href="/guide/"{cur("guide")}>이용 안내</a></nav></div></header>')

    def foot(self, active):
        return (f'<footer class="site-footer"><div class="wrap"><strong>{BRAND_KO} {BRAND_EN}</strong> · 운동하고, 사우나하고, 한 세트.'
                f'<div class="flinks"><a href="/about/">소개</a><a href="/guide/">이용 안내</a><a href="/archive/">조감도 아카이브</a><a href="/events/">이벤트</a><a href="/hyrox-seoul/">HYROX 서울</a><a href="/faq/">자주 묻는 질문</a><a href="/corrections/">정정 요청</a>'
                f'<a href="/privacy/">개인정보 처리 안내</a></div>'
                f'<p>가격·휴무일은 바뀔 수 있다. 모든 항목의 확인일과 출처를 확인하고, 이용 전 업장에 한 번 더 문의해야 한다.</p>'
                f'<p>© 2026 {BRAND_EN}</p></div></footer>')

    def add(self, path, title, desc, body, active="", scripts=()):
        sc = "".join(f'<script src="/assets/js/{s}?v={asset_ver("js/"+s)}" defer></script>' for s in scripts)
        out_path = path
        self.pages[out_path] = (self.head(title, desc, path) + f'<body>{self.nav(active)}<main id="main" class="wrap">{body}</main>{self.foot(active)}{sc}</body></html>')

def badge(txt, cls=""): return f'<span class="badge {cls}">{E(txt)}</span>'
def v_badges(v, full=False):
    b = []
    if v["ad"]: b.append(badge("광고", "badge-ad"))
    b.append(badge("박스" if v["kind_key"] == "box" else "사우나", "badge-box" if v["kind_key"] == "box" else "badge-sauna"))
    b.append(badge(v["kind"]))
    b.append(badge("확인됨" if v["status"] == "verified" else "일부 확인", "badge-ok" if v["status"] == "verified" else "badge-part"))
    if v["stale"]: b.append(badge("오래됨", "badge-old"))
    for t in v["tags"]: b.append(badge(t))
    return '<div class="chips">' + "".join(b) + "</div>"
def off_marks(v):
    o = v["off"]; out = []
    if o["cf"]: out.append(f'크로스핏 공식 어필리에이트 확인({o["date"]})')
    if o["hyrox"]: out.append(f'하이록스 공식 지점 목록 확인({o["date"]})')
    return out
def off_html(v):
    m = off_marks(v)
    return "".join(f'<div class="off">✓ {E(t)}</div>' for t in m)
SUN_TXT = {"yes": "일요일 운영 확인", "no": "일요일 휴무 확인", "unk": "일요일 확인 중"}

def cav(v): return f'<div class="meta cav">※ {E(v["price_caveat"])}</div>' if v["price_caveat"] and v["price"] else ""

def venue_card(v):
    price = (f'<div class="price">{E(v["price_label"])} {E(v["price"])}</div>' if v["price"]
             else '<div class="price unk">요금 확인 중</div>')
    attrs = (f'data-city="{v["city_key"]}" data-type="{v["kind_key"]}" data-sunday="{v["sunday"]}" data-price="{1 if v["price"] else 0}" data-cf="{1 if v["off"]["cf"] else 0}" data-hyrox="{1 if v["off"]["hyrox"] else 0}" data-sub="{v["sub"]}" data-order="{v["order"]}" data-date="{v["last_verified"].isoformat()}" data-title="{E(v["title"])}" '
             f'data-name="{E((v["title"] + " " + v["name"] + " " + v["location"]).lower())}"')
    return (f'<article class="card" {attrs}>{v_badges(v)}<h3><a href="/finder/{v["slug"]}/">{E(v["title"])}</a></h3>'
            f'<div class="loc">{E(v["location"])}</div>{price}{cav(v)}<div class="loc">{SUN_TXT[v["sunday"]]}</div>{off_html(v)}'
            f'<div class="meta">확인일 {dot(v["last_verified"])} · 출처 {E(host(v["source"]))}</div></article>')

def mini_card(v):
    price = (f'{E(v["price_label"])} {E(v["price"])}' if v["price"] else "요금 확인 중")
    return (f'<article class="card">{v_badges(v)}<h3><a href="/finder/{v["slug"]}/">{E(v["title"])}</a></h3>'
            f'<div class="loc">{E(v["location"])}</div><div class="price{"" if v["price"] else " unk"}">{price}</div>{off_html(v)}'
            f'<div class="meta">확인일 {dot(v["last_verified"])}</div></article>')

def home_card(v):
    price = (f'{E(v["price_label"])} {E(v["price"])}' if v["price"] else "요금 확인 중")
    area = v["location"].split(" · ", 1)[0]
    if re.search(r"\d", area):
        match = re.match(r"^((?:서울|부산|제주|경기)\s+(?:[가-힣]+시\s+)?[가-힣]+(?:구|군|시))", area)
        if match: area = match.group(1)
    lv = v["last_verified"]
    return (f'<article class="card home-card"><h3><a href="/finder/{v["slug"]}/">{E(v["title"])}</a></h3>'
            f'<div class="home-info"><span>{E(area)}</span> <span aria-hidden="true">·</span> '
            f'<span>{E(v["kind"])}</span> <span aria-hidden="true">·</span> '
            f'<span class="price{"" if v["price"] else " unk"}">{price}</span></div>'
            f'<div class="home-date">{lv.month}.{lv.day} 확인</div></article>')

def event_card(e, today, detail_link=True):
    d = (e["start"] - today).days
    badge_txt = "진행 중" if e["start"] <= today <= e["end"] else ("임박" if 0 <= d <= 7 else "")
    rows = "".join(f'<tr><th>{E(k)}</th><td>{E(val)}</td></tr>' for k, val in e["facts"])
    link = f'<a href="/hyrox-seoul/">레이스 위크 가이드 →</a> · ' if e["feature"] and detail_link else ""
    return (f'<article class="card" id="{e["slug"]}" data-end="{e["end"].isoformat()}"><div class="chips">{badge(e["kind"])}{badge(badge_txt, "badge-part") if badge_txt else ""}</div>'
            f'<h3>{E(e["title"])}</h3><div class="when" data-dday="{e["start"].isoformat()}">{span(e["start"], e["end"], today.year)}{(" · <b>" + ("D-DAY" if d == 0 else f"D-{d}") + "</b>") if d >= 0 else ""}</div>'
            f'<table class="facts"><tbody>{rows}</tbody></table>' + (f'<p class="muted">{E(e["note"])}</p>' if e["note"] else "") +
            f'<div class="meta">{link}<a href="{E(e["url"])}" rel="noopener noreferrer">{E(e["source_label"])}</a> · 확인일 {dot(e["last_verified"])}</div></article>')

# ------------------------------------------------------------------ pages
HYROX_STATIONS = [("SkiErg", "스키에르그", "1,000m"), ("Sled Push", "슬레드 푸시", "50m"), ("Sled Pull", "슬레드 풀", "50m"),
                  ("Burpee Broad Jump", "버피 브로드 점프", "80m"), ("Rowing", "로잉", "1,000m"), ("Farmers Carry", "파머스 캐리", "200m"),
                  ("Sandbag Lunges", "샌드백 런지", "100m"), ("Wall Balls", "월볼", "100회")]


# ------------------------------------------------------------------ magazine (articles)
ARTICLES = [  # newest first; source markdown lives in content/articles/
    {"slug": "shin-weight-loss-lean-mass", "file": "shin-weight-loss-lean-mass.md", "date": "2026-10-17", "category": "웰니스 리포트"},
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

def article_html(a):
    d = dt.date.fromisoformat(a["date"])
    parts = []
    for blk in re.split(r"\n\s*\n", a["본문"]):
        blk = blk.strip()
        if not blk: continue
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
    return (f'<article class="article"><header class="a-head"><span class="eyebrow">{E(a["category"])}</span><h1>{E(a["title"])}</h1>'
            f'<p class="deck">{E(a["데크"])}</p><div class="byline">에디터 천한샘 · {dot(d)}</div></header>'
            f'<aside class="summary" aria-label="3줄 요약"><h2>3줄 요약</h2><ol>{summ}</ol></aside>'
            f'<div class="a-body">{"".join(parts)}</div>'
            f'<section class="refs"><h2>출처</h2><ol>{"".join(refs)}</ol></section>'
            f'<div class="note">{E(a["안내 문구"])}</div>'
            f'<p><a href="/magazine/">← 매거진 목록</a></p></article>')

def build(site, venues, events, today):
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
        btxt = "진행 중" if started else ("D-DAY" if d == 0 else f"D-{d}")
        ds = f'{feat["start"].month}/{feat["start"].day}~{feat["end"].month}/{feat["end"].day}'
        race_strip = (f'<div class="race-strip" data-end="{feat["end"].isoformat()}">'
                      f'<a class="race-strip-link race-strip-narrow" href="/hyrox-seoul/"><span>HYROX 서울</span><span>·</span>'
                      f'<b data-dday="{feat["start"].isoformat()}">{btxt}</b><span>·</span><span>{ds}</span><span>·</span><span>가이드 ›</span></a>'
                      f'<a class="race-strip-link race-strip-wide" href="/hyrox-seoul/"><span>HYROX 서울</span><span>·</span>'
                      f'<b data-dday="{feat["start"].isoformat()}">{btxt}</b><span>·</span><span>{ds}</span><span>·</span><span>KINTEX</span><span>·</span><span>가이드 ›</span></a>'
                      f'</div>')
    body = f'''
<section class="hero home-hero"><h1>운동할 곳, 사우나 할 곳, 공식정보로 확인한다.</h1>
<a class="btn btn-a region-pick" href="/finder/">지역 선택</a></section>

<section class="sec home-results"><div class="sec-h"><h2>확인한 곳</h2><a href="/finder/">{len(venues)}곳 모두 보기 →</a></div>
<div class="grid home-grid">{"".join(home_card(v) for v in preview)}</div>
<p class="muted trust">공식 페이지 확인일·출처 표시</p></section>

{race_strip}
<p class="safety-line">{SAFETY}</p>

<section class="sec home-mag"><div class="sec-h"><h2>매거진</h2><a href="/magazine/">전체 보기</a></div>
<div class="grid">{"".join(f'<article class="card"><div class="chips">{badge(a["category"])}</div><h3><a href="/magazine/{a["slug"]}/">{E(a["title"])}</a></h3><div class="meta">에디터 천한샘 · {dot(dt.date.fromisoformat(a["date"]))}</div></article>' for a in [parse_article(m) for m in ARTICLES])}</div></section>

<section class="sec"><div class="sec-h"><h2>그림으로 보기</h2><a href="/guide/#pictures">크게 보기</a></div>
<p class="muted">크로스핏 박스·부티크 사우나 · 호텔 사우나, 처음 가는 분을 위한 손그림 안내다. 일반적인 예시이고 실제 업장과 다를 수 있어요.</p>
<div class="illrow">{"".join(f'<a class="illc" href="/guide/#{k}"><picture><source media="(min-width:720px)" srcset="/assets/img/guide/{f}.svg"><img src="/assets/img/guide/{f}-thumb.png" width="252" height="315" loading="lazy" decoding="async" alt="{E(d)}"></picture><span><b>{E(t)}</b><em>그림 보기 ›</em></span></a>' for k, f, t, d in ILLUSTRATIONS)}</div></section>

<section class="sec"><div class="sec-h"><h2>조감도 아카이브</h2><a href="/archive/">전체 보기</a></div>
<p class="muted">업장 공식 안내에 글자로 공개된 층·구역 구성을 읽고, 위에서 비스듬히 내려다본 조감도 스타일로 새로 그린 손그림이다. 구역 배치는 개념도다. 실제와 다를 수 있다.</p>
<div class="illrow arcrow">{"".join(f'<a class="illc" href="/archive/#{vs}"><picture><source media="(min-width:720px)" srcset="/assets/img/archive/{f}-mid.png"><img src="/assets/img/archive/{f}-thumb.png" width="252" height="315" loading="lazy" decoding="async" alt="{E(d)}"></picture><span><b>{E(st)}</b><em>그림 보기 ›</em></span></a>' for f, vs, t, st, d, sl, su in ARCHIVE)}</div>
<p><a class="btn btn-d" href="/archive/">조감도 아카이브 전체 보기 ›</a></p></section>'''
    site.add("/", f"{BRAND_KO} {BRAND_EN} — 크로스핏·HYROX·사우나 웰니스 매거진", "크로스핏·HYROX·호텔/부티크 사우나. 공식 출처와 확인일을 붙인 드롭인·사우나 파인더와 레이스 일정.", body, "home", ("dday.js",))

    # ---- finder
    ordered = sorted(venues, key=lambda v: (not v["ad"], v["status"] != "verified", v["order"]))
    cnt_box = sum(v["kind_key"] == "box" for v in venues); cnt_sauna = len(venues) - cnt_box
    seg = lambda g, items: '<div class="seg" data-seg="%s">' % g + "".join(f'<button type="button" data-v="{k}" aria-pressed="false">{E(t)}</button>' for k, t in items) + "</div>"
    fbody = f'''
<div class="finder-head"><h1>{FINDER_LABEL}</h1>
<p class="lead">드롭인으로 갈 수 있는 크로스핏·HYROX 박스와 호텔·부티크 사우나, 대형 스파·온천. 지금 {len(venues)}곳(박스 {cnt_box} · 사우나 {cnt_sauna})을 확인했고, 계속 늘려 간다.</p>
<details class="howto"><summary>필터 쓰는 법</summary><ul>
<li><strong>도시·유형</strong>: 가고 싶은 지역과 박스/사우나를 먼저 고른다. 사우나는 호텔 사우나·부티크 사우나·대형 스파·온천으로 한 번 더 나눠 두었다.</li>
<li><strong>일요일</strong>: 공식 페이지에 적혀 있는 곳만 '운영/휴무 확인'으로 보인다. 적혀 있지 않으면 '확인 중'이다.</li>
<li><strong>요금 확인된 곳만</strong>: 공식 페이지에서 요금을 읽은 곳만 남긴다.</li>
<li><strong>공식 목록</strong>: 크로스핏·하이록스 공식 목록에서 확인일에 직접 본 곳만 남긴다. 표시가 없는 곳은 확인 중이다.</li>
<li><strong>정렬</strong>: 기본 순서, 최근 확인한 순, 이름순 중에서 고른다.</li>
</ul><p class="muted">더 자세한 설명은 <a href="/guide/">이용 안내</a>와 <a href="/faq/">자주 묻는 질문</a>에 있다.</p></details></div>
<form class="filters" aria-label="필터">
 <div class="fgroup"><span>이름·지역 검색</span><input id="f-q" type="search" placeholder="예: 한남, 부산, 호텔" autocomplete="off"></div>
 <div class="fgroup"><span>도시</span>{seg("city", [("", "전체"), ("seoul", "서울"), ("busan", "부산"), ("jeju", "제주"), ("gyeonggi", "경기")])}</div>
 <div class="fgroup"><span>유형</span>{seg("type", [("", "전체"), ("box", "크로스핏·HYROX 박스"), ("sauna", "사우나")])}</div>
 <div class="fgroup"><span>사우나 종류</span><select id="f-sub"><option value="">전체</option><option value="hotel">호텔 사우나</option><option value="boutique">부티크 사우나</option><option value="spa">대형 스파·온천</option></select></div>
 <div class="fgroup"><span>정렬</span><select id="f-sort"><option value="">기본 순서</option><option value="date">최근 확인한 순</option><option value="name">이름순</option></select></div>
 <div class="fgroup"><span>일요일</span><select id="f-sunday"><option value="">전체</option><option value="yes">운영 확인된 곳</option><option value="no">휴무 확인된 곳</option><option value="unk">확인 중</option></select></div>
 <div class="fgroup"><span>요금</span><label class="chk"><input id="f-price" type="checkbox"> 요금 확인된 곳만</label></div>
 <div class="fgroup"><span>공식 목록</span><label class="chk"><input id="f-hyrox" type="checkbox"> 하이록스 지점(공식 목록 확인)</label> <label class="chk"><input id="f-cf" type="checkbox"> 크로스핏 어필리에이트(공식 확인)</label></div>
 <div class="fgroup"><span>&nbsp;</span><button id="f-reset" data-reset type="button" class="btn btn-d">필터 초기화</button></div>
</form>
<p class="count" id="f-count" aria-live="polite">총 {len(venues)}곳</p>
<div class="grid" id="finder-list">{"".join(venue_card(v) for v in ordered)}</div>
<div class="empty" id="f-empty" hidden><p>조건에 맞는 곳이 아직 확인되지 않았다.</p><p class="muted">필터를 줄이거나 아래 버튼으로 처음부터 다시 고르면 된다. 확인한 곳은 계속 추가된다.</p><button type="button" class="btn btn-d" data-reset>필터 초기화</button></div>
<div class="note">'확인됨'은 업장 공식 페이지에서 해당 정보를 직접 읽었다는 뜻이고, '일부 확인'은 주소 같은 일부만 확인했다는 뜻이다. 일요일 칸은 공식 페이지에 적혀 있을 때만 채운다. '공식 어필리에이트 확인'과 '하이록스 공식 지점 목록 확인'은 해당 공식 페이지·목록에서 확인일에 직접 본 곳에만 붙인다. 표시가 없는 곳은 아직 확인 중이라는 뜻이다.</div>
<noscript><p class="muted">필터는 자바스크립트가 켜져 있어야 작동한다. 목록은 그대로 볼 수 있다.</p></noscript>'''
    site.add("/finder/", f"{FINDER_LABEL} — 드롭인·사우나 파인더", "도시·유형·일요일 운영·요금 확인 여부로 고르는 크로스핏·HYROX 박스와 호텔·부티크 사우나. 모든 항목에 확인일과 출처를 달았다.", fbody, "finder", ("finder.js",))

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
                cell = f'<td class="unk">확인 중</td>' if not val.strip() else f'<td>{E(val)}</td>'
                out.append(f'<tr><th>{E(k)}</th>{cell}</tr>')
            return "".join(out)
        links = []
        if v["website"] and "map.naver.com" not in v["website"]:
            links.append(f'<a class="btn btn-d" href="{E(v["website"])}" rel="{"sponsored " if v["ad"] else ""}noopener noreferrer">공식 사이트</a>')
        if v["booking"] and v["booking"] != v["website"]:
            links.append(f'<a class="btn btn-d" href="{E(v["booking"])}" rel="noopener noreferrer">예약 페이지</a>')
        q = quote(v["title"] + (" " + v["address"] if v["address"] else ""))
        links.append(f'<a class="btn btn-d" href="https://map.naver.com/p/search/{q}" rel="noopener noreferrer">지도에서 검색</a>')
        srcs = [f'<a href="{E(v["source"])}" rel="noopener noreferrer">{E(host(v["source"]))}</a>'] + [f'<a href="{E(u)}" rel="noopener noreferrer">{E(l)}</a>' for l, u in v["extra_sources"]]
        adnote = (f'<div class="note ad"><strong>광고</strong> · 이 항목에는 광고 표시가 붙어 있다. 확인 정보는 공식 출처 기준이다. '
                  f'{E(v["ad_note"])}</div>' if v["ad"] else "")
        tips = ("<h2>이용 팁·주의</h2><ul class='clean'>" + "".join(f"<li>{E(t)}</li>" for t in v["tips"]) + "</ul>") if v["tips"] else ""
        unv = ("<h2>아직 확인되지 않은 항목</h2><ul class='clean'>" + "".join(f"<li>{E(t)}</li>" for t in v["unverified"]) + "</ul>") if v["unverified"] else ""
        stale = (f'<div class="note">확인한 지 {STALE_DAYS}일이 지났다. 이용 전에 업장에 다시 확인해야 한다.</div>' if v["stale"] else "")
        safety = f'<p class="safety">{SAFETY}</p>' if v["kind_key"] == "sauna" else ""
        offrows = "".join(f'<tr><th>공식 목록 확인</th><td>{E(t)}</td></tr>' for t in off_marks(v))
        extra_addr = f'<tr><th>주소</th><td>{E(v["address"])}</td></tr>' if v["address"] and not any(k == "주소" for k, _ in v["facts"]) else ""
        sunrow = f'<tr><th>일요일</th><td{"" if v["sunday"] != "unk" else " class=unk"}>{SUN_TXT[v["sunday"]] if v["sunday"] != "unk" else "확인 중"}</td></tr>' if v["kind_key"] == "box" or v["sunday"] != "unk" else ""
        if v["kind_key"] == "box" and any("일요일" in k or "휴무" in k for k, _ in v["facts"]) and v["sunday"] != "unk": sunrow = ""
        if v["kind_key"] == "box" and v["sunday"] == "unk" and any("일요일" in k for k, _ in v["facts"]): sunrow = ""
        _ar = ARCHIVE_BY_VENUE.get(v["slug"])
        arch_blk = (f'<section class="archbox"><h2>손그림 조감도</h2><a class="illc" href="/archive/#{v["slug"]}"><picture><img src="/assets/img/archive/{_ar[0]}-thumb.png" width="252" height="315" loading="lazy" decoding="async" alt="{E(_ar[4])}"></picture>'
                    f'<span><b>{E(_ar[2])}</b><em>공식 안내를 바탕으로 한 손그림 · 눌러서 보기 ›</em></span></a></section>') if _ar else ""
        city_group = by_city.get(v["city_key"], [])
        same_city = len(city_group) > 1
        if same_city:
            idx = next((i for i, x in enumerate(city_group) if x["slug"] == v["slug"]), 0)
            nxt = city_group[(idx + 1) % len(city_group)]
        else:
            idx = next((i for i, x in enumerate(all_ordered) if x["slug"] == v["slug"]), 0)
            nxt = all_ordered[(idx + 1) % len(all_ordered)]
        next_label = f'같은 지역 다음 업장: {E(nxt["title"])} ›' if same_city else f'다음 업장: {E(nxt["title"])} ›'
        next_html = f'<p class="next-venue"><a href="/finder/{nxt["slug"]}/">{next_label}</a></p>'
        body = f'''<div class="detail"><p class="crumb"><a href="/finder/">{FINDER_LABEL}</a> › {E(v["city_ko"])}</p>
{v_badges(v)}<h1>{E(v["title"])}</h1><p class="muted">{E(v["location"])}</p>{adnote}{stale}
<p class="lead">{E(v["summary"])}</p>
<table class="facts"><tbody>{rows()}{extra_addr}{sunrow}{offrows}</tbody></table>
{tips}{unv}{arch_blk}
<div class="verify"><p><strong>마지막 확인일 {dot(v["last_verified"])}</strong></p><p>출처: {" · ".join(srcs)}</p>
<p>정보가 다르면 <a href="/corrections/">정정 요청</a>으로 알리면 된다. 가격·휴무는 바뀔 수 있다.</p></div>
{next_html}
<div class="btnrow">{"".join(links)}</div>{safety}</div>'''
        site.add(f"/finder/{v['slug']}/", f"{v['title']}", f"{v['title']} — {v['summary']}"[:150], body, "finder")

    # ---- events
    cards = "".join(event_card(e, today) for e in upcoming)
    if not upcoming: cards = '<div class="empty"><p>지금은 공식 페이지에서 일정을 확인한 대회가 없다.</p><p class="muted">확인되는 대로 추가한다.</p></div>'
    ebody = f'''<div class="page"><h1>이벤트 캘린더</h1>
<p class="lead">주최측 공식 페이지에서 일정을 확인한 대회만 싣는다.</p></div>
<div class="ev">{cards}</div>
<div class="note">집계 사이트에만 올라온 일정은 주최측 확인 전까지 싣지 않는다. 참가비는 공식 페이지에서 확인한 값만 적는다. <a href="/events.ics">전체 일정 .ics 받기</a></div>'''
    site.add("/events/", "이벤트 캘린더", "HYROX 서울, 어반웨이브, 크로스핏 오픈 등 공식 페이지에서 확인한 대회 일정.", ebody, "events", ("dday.js",))

    # ---- HYROX feature
    trs = "".join(f"<tr><td>{i}</td><td>1km</td><td>{E(en)} <span class='muted'>({E(ko)})</span></td><td>{E(d)}</td></tr>" for i, (en, ko, d) in enumerate(HYROX_STATIONS, 1))
    hv = "".join(mini_card(v) for v in hyrox_v)
    hbody = f'''<section class="hero"><span class="eyebrow">RACE WEEK GUIDE</span><h1>{E(feat["title"])}</h1>
<div class="dday" data-dday="{feat["start"].isoformat()}"><b>D-{max((feat["start"] - today).days, 0)}</b>{span(feat["start"], feat["end"], today.year)} · KINTEX</div>
<p>처음 나가는 분을 위해 레이스 구성을 한눈에 정리했다. 일정은 공식 페이지 기준이다.</p>
<div class="btnrow"><a class="btn btn-a" href="{E(feat["url"])}" rel="noopener noreferrer">공식 페이지 (접수·참가비)</a></div></section>
<div class="page"><h2>한눈에 보기</h2>
<table class="facts"><tbody>{"".join(f"<tr><th>{E(k)}</th><td>{E(val)}</td></tr>" for k, val in feat["facts"])}</tbody></table>
<h2>레이스 구성</h2>
<p>1km 러닝과 기능성 스테이션 1개를 한 세트로 8번 반복한다. 순서와 거리는 아래와 같다.</p>
<table class="t"><thead><tr><th>#</th><th>러닝</th><th>스테이션</th><th>거리·횟수</th></tr></thead><tbody>{trs}</tbody></table>
<h2>어떤 종목으로 나가는가</h2>
<ul><li>Open / Pro (개인)</li><li>Doubles / Doubles Pro (2인)</li><li>Relay: 본인 포함 4명 팀, 각자 2×1km와 스테이션 1개</li></ul>
<div class="note">종목·성별별 무게 표는 공식 페이지에 있다. 시즌별로 기준이 바뀔 수 있으니 출전 전에 <a href="https://hyrox.com/the-fitness-race/" rel="noopener noreferrer">HYROX 공식 안내</a>를 다시 확인해야 한다. 접수 상태와 참가비는 공식 페이지 본문에서 확인하지 못해 싣지 않았다.</div>
<h2>미리 훈련해 볼 곳</h2><p>공식 페이지에서 HYROX 수업이나 드롭인을 확인한 곳이다. 요금은 확인된 것만 적는다.</p></div>
<div class="grid g3">{hv}</div>
<div class="page"><p class="muted">고양·일산권 사우나와 박스는 공식 페이지에서 확인한 곳부터 <a href="/finder/?city=gyeonggi">Finder</a>에 추가한다.</p>
<p class="muted">{SAFETY}</p><p class="muted">확인일 {dot(feat["last_verified"])} · 출처 <a href="{E(feat["url"])}" rel="noopener noreferrer">HYROX Korea</a> · <a href="https://hyrox.com/the-fitness-race/" rel="noopener noreferrer">HYROX 공식</a></p></div>'''
    site.add("/hyrox-seoul/", "HYROX 서울 레이스 위크 가이드 (11/13–15, KINTEX)", "에어아시아 하이록스 서울 일정, 8개 스테이션 구성, 드롭인 가능한 훈련 장소.", hbody, "hyrox", ("dday.js",))


    # ---- magazine
    arts = [parse_article(m) for m in ARTICLES]
    for a in arts:
        site.add(f"/magazine/{a['slug']}/", a["title"], a["데크"][:140], article_html(a), "magazine")
    mcards = "".join(f'<article class="card"><div class="chips">{badge(a["category"])}</div><h3><a href="/magazine/{a["slug"]}/">{E(a["title"])}</a></h3>'
                     f'<div class="loc">{E(a["데크"][:90])}…</div><div class="meta">에디터 천한샘 · {dot(dt.date.fromisoformat(a["date"]))}</div></article>' for a in arts)
    site.add("/magazine/", "매거진", "한셋 에디터가 공식 자료와 논문을 확인해 쉽게 풀어 쓴 웰니스 리포트.",
             f'<div class="page"><h1>매거진</h1><p class="lead">논문과 공식 자료를 확인해 쉽게 풀어 쓴 웰니스 리포트다. 모든 글에 출처를 달아 둔다.</p></div><div class="grid">{mcards}</div>', "magazine")

    # ---- archive (조감도 아카이브)
    items = "".join(
        f'<article class="arc" id="{vs}"><a class="arcimg" href="/assets/img/archive/{f}.svg" target="_blank" rel="noopener" aria-label="{E(t)} 그림 크게 보기(새 탭)">'
        f'<picture><source media="(min-width:720px)" srcset="/assets/img/archive/{f}-mid.png"><img src="/assets/img/archive/{f}-thumb.png" width="252" height="315" loading="lazy" decoding="async" alt="{E(d)}"></picture></a>'
        f'<div class="arcbody"><h2>{E(t)}</h2><p class="meta">구역 배치는 개념도 · 공식 안내를 바탕으로 한 손그림 · 실제와 다를 수 있다</p>'
        f'<p class="meta">출처 <a href="{E(su)}" rel="noopener noreferrer">{E(sl)}</a> · 확인일 {ARCHIVE_DATE}</p>'
        f'<div class="btnrow"><a class="btn btn-d" href="/assets/img/archive/{f}.svg" target="_blank" rel="noopener">눌러서 크게 보기 ↗</a><a class="btn btn-d" href="/finder/{vs}/">업장 정보 보기</a></div></div></article>'
        for f, vs, t, st, d, sl, su in ARCHIVE)
    abody = f'''<div class="page"><h1>조감도 아카이브</h1>
<p class="lead">업장 공식 홈페이지·공식 안내에 글자로 공개된 층과 구역 구성을 읽고, 한셋이 위에서 비스듬히 내려다본 조감도 스타일(아이소메트릭)로 새로 그린 손그림이다.</p>
<ul><li>사진이나 공식 안내도 원본을 옮기거나 따라 그리지 않았다. 공식 안내에 없는 구조는 그리지 않는다.</li>
<li>구역 배치는 개념도다. 층·구역의 이름과 순서만 공식 안내를 따랐고, 위치·크기는 실제와 다를 수 있다.</li>
<li>글씨가 작으면 그림을 눌러 새 탭에서 크게 보면 된다(두 손가락으로 확대할 수 있다).</li></ul></div>
<div class="arcgrid">{items}</div>
<div class="page"><p class="muted">공식 층별·구역 안내가 글자로 공개된 곳부터 하나씩 추가한다. {SAFETY}</p></div>'''
    site.add("/archive/", "조감도 아카이브", "업장 공식 안내를 바탕으로 한 조감도 스타일 손그림 모음(구역 배치는 개념도). 실제와 다를 수 있고, 모든 그림에 출처와 확인일이 있다.", abody, "guide")

    # ---- static pages
    for slug, title, desc, active in [("guide", "이용 안내", "한셋의 파인더·배지·확인일을 읽는 방법과 이용 전에 알아 둘 점.", "guide"),
                                      ("faq", "자주 묻는 질문", "한셋의 확인 방식, '확인 중'의 뜻, 정정 요청 방법 등 자주 묻는 질문.", "guide"),
                                      ("about", "소개", "한셋은 크로스핏·HYROX·호텔/부티크 사우나를 공식 출처와 확인일로 정리하는 웰니스 매거진이다.", "about"),
                                       ("privacy", "개인정보 처리 안내", "한셋은 추적 도구 없이 운영된다.", "about"),
                                       ("corrections", "정정 요청", "틀린 정보를 정정 요청으로 받는다.", "about")]:
        frag = (ROOT / f"content/pages/{slug}.html").read_text(encoding="utf-8")
        if slug == "guide":
            figs = "".join(f'<figure class="fig" id="{k}"><a class="figlink" href="/assets/img/guide/{f}.svg" target="_blank" rel="noopener" aria-label="{E(t)} 그림 크게 보기(새 탭)"><img src="/assets/img/guide/{f}.svg" width="1080" height="1350" loading="lazy" decoding="async" alt="{E(d)}"></a><figcaption><strong>{E(t)}</strong><br>일반 안내 그림이다. 실제 업장과 다를 수 있어요. <a href="/assets/img/guide/{f}.svg" target="_blank" rel="noopener">눌러서 크게 보기 ↗</a></figcaption></figure>' for k, f, t, d in ILLUSTRATIONS)
            frag = frag.replace("<!--ILLUSTRATIONS-->", f'<h2 id="pictures">그림으로 보기</h2><p>처음 가는 분을 위한 손그림 안내다. 일반적인 예시이고, 실제 업장의 구조·방식과 다를 수 있다. 글씨가 작으면 그림을 눌러 새 탭에서 크게 보면 된다(두 손가락으로 확대할 수 있다).</p><div class="figs">{figs}</div>')
        site.add(f"/{slug}/", title, desc, f'<div class="page">{frag}</div>', active)

    site.add("/404.html", "페이지를 찾을 수 없다", "페이지를 찾을 수 없다.", f'<div class="page"><h1>페이지를 찾을 수 없다</h1><p><a href="/">홈으로</a> 또는 <a href="/finder/">{FINDER_LABEL}</a>로 이동하면 된다.</p></div>')

def asset_ver(rel):
    import hashlib
    return hashlib.md5((ROOT / "static" / rel).read_bytes()).hexdigest()[:8]

ILLUSTRATIONS = [
    ("box", "crossfit-box-plan", "크로스핏 박스, 이렇게 생겼다", "크로스핏 박스의 일반적인 구조를 위에서 내려다본 손그림. 입구와 접수 데스크, 락커·탈의 공간, 화이트보드, 와드 구역, 리그, 오픈짐·바벨 랙, 로잉·바이크 구역의 일반적인 배치 예시이며 실제 업장과 다를 수 있어요."),
    ("dropin", "dropin-steps", "드롭인, 이렇게 이용한다", "크로스핏 드롭인 이용 순서 손그림. 1 공식 페이지에서 시간표와 요금 확인, 2 예약·문의, 3 도착해서 접수, 4 수업 참여, 5 정리와 결제. 수업 방식과 요금은 업장마다 다르다."),
    ("sauna", "sauna-guide", "사우나, 이렇게 이용한다", "사우나 이용 순서와 건식·습식 사우나, 샤워, 냉수욕, 휴게 공간의 일반적인 배치 예시 손그림. 심혈관 질환·임신·음주 후·컨디션 불량 시에는 사우나와 냉수욕을 피하거나 의사와 상담해야 한다. 업장마다 시설이 다르다."),
]

def ics(events):
    def esc(s): return s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//HANSET//events//KO", "CALSCALE:GREGORIAN", "X-WR-CALNAME:한셋 이벤트"]
    for e in events:
        lines += ["BEGIN:VEVENT", f"UID:{e['slug']}@hanset", f"DTSTAMP:{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}",
                  f"DTSTART;VALUE=DATE:{e['start']:%Y%m%d}", f"DTEND;VALUE=DATE:{(e['end'] + dt.timedelta(days=1)):%Y%m%d}",
                  f"SUMMARY:{esc(e['title'])}", f"LOCATION:{esc(e['venue'] + ', ' + e['city'])}", f"URL:{e['url']}",
                  "DESCRIPTION:" + esc("일정은 공식 페이지 기준이다. 참가비·접수는 공식 페이지에서 확인해야 한다."), "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"

def subset_fonts(dist):
    try:
        from fontTools import subset
        from fontTools.ttLib import TTFont
    except ImportError:
        print("WARN: fonttools not installed; skipping font subsetting (site falls back to system Korean fonts). Use .venv/bin/python.")
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
# (파일, 업장 slug, 제목, 짧은 제목, 대체텍스트, 출처 라벨, 출처 URL)
ARCHIVE = [
    ("archive-sisu-house", "sisu-house", "시수하우스 조감도", "시수하우스", "시수하우스 공식 공간 안내의 층 구성(1F Welcoming Zone, 1.5F Touch Zone, 2-3F Private Zone)과 리추얼 순서를 쌓아 올린 아이소메트릭 손그림. 구역 배치는 개념도다.", "sisu.house", "https://sisu.house/about"),
    ("archive-spa-haesurang", "spa-haesurang-busan", "스파 해수랑 조감도", "스파 해수랑", "스파 해수랑 공식 편의시설 안내의 층 표기(2F 로비, 3~4F 파우더룸, 4~5F 락커룸, 5F 찜질방, 7F 노천탕)를 층별로 쌓아 올린 아이소메트릭 손그림. 6층은 공식 안내가 없고 구역 배치는 개념도다.", "spahaesurang.co.kr", "https://spahaesurang.co.kr/amenities"),
    ("archive-heosimcheong", "heosimcheong-busan", "허심청 조감도", "허심청(호텔농심)", "호텔농심 허심청 공식 층별 시설 안내(B1·B2 주차장, 1F 식당, 2F 연회, 3F 찜질방, 4F 락카·안내데스크, 5F 온천탕)를 쌓아 올린 아이소메트릭 손그림. 구역 배치는 개념도다.", "hotelnongshim.com", "https://www.hotelnongshim.com/kr/?pCode=Hurshimchung"),
    ("archive-termeden", "termeden-icheon", "테르메덴 조감도", "테르메덴", "테르메덴 공식 시설 소개의 구역(실내 풀&스파, 온천대욕장, 찜질스파, 실외 풀&스파)을 한 장에 모은 아이소메트릭 손그림. 칸의 위치는 실제 배치가 아니다.", "termeden.com", "https://termeden.com/poolspa_explain.html"),
    ("archive-orakai-cheonggyesan", "orakai-cheonggyesan", "오라카이 청계산 조감도", "오라카이 청계산", "오라카이 청계산 호텔 공식 안내의 지하 1층 사우나(건식·습식 사우나, 여러 탕, 세신 현장 접수)와 지하 2층 전기차 충전소를 쌓아 올린 아이소메트릭 손그림. 사우나 안의 구체적인 배치는 그리지 않았다.", "cgs.orakaihotels.com", "https://cgs.orakaihotels.com/kr/about/facilities_sauna.asp"),
    ("archive-crossfit-super-sapiens", "crossfit-super-sapiens", "슈퍼사피엔스 조감도", "크로스핏 슈퍼사피엔스", "크로스핏 슈퍼사피엔스 공식 시설 안내의 층 구성(1F 메인 운동 공간, 2F 샤워실·락커룸·보조 운동 공간, 3F 태닝·스태프 공간)을 쌓아 올린 아이소메트릭 손그림. 층 안의 배치는 개념도다.", "crossfitsupersapiens.com", "https://crossfitsupersapiens.com/our-facility"),
]
ARCHIVE_BY_VENUE = {a[1]: a for a in ARCHIVE}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", action="store_true"); ap.add_argument("--ads"); ap.add_argument("--today"); ap.add_argument("--out", default=str(DIST))
    a = ap.parse_args()
    today = ymd(a.today) if a.today else dt.datetime.now(ZoneInfo("Asia/Seoul")).date()
    dist = Path(a.out)
    venues, events, skipped, ev_skipped = load(today, a.ads)
    site = Site(today, a.release)
    build(site, venues, events, today)
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
    urls = [p for p in site.pages if p != "/404.html"]
    if a.release:
        (dist / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n")
        (dist / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(f"<url><loc>{SITE_URL}{u}</loc><lastmod>{today}</lastmod></url>" for u in urls) + "</urlset>\n")
    else:
        (dist / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    (dist / "_headers").write_text("/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: strict-origin-when-cross-origin\n  X-Frame-Options: DENY\n  Permissions-Policy: camera=(), microphone=(), geolocation=()\n"
        "  Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; font-src 'self'; connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'\n"
        "/assets/*\n  Cache-Control: public, max-age=86400\n/*.html\n  Cache-Control: public, max-age=0, must-revalidate\n")
    subset_fonts(dist)
    bad = leak_check(dist)
    total = sum(p.stat().st_size for p in dist.rglob("*") if p.is_file())
    print(f"built {len(site.pages)} pages, {len(venues)} venues ({sum(v['kind_key']=='box' for v in venues)} boxes, {sum(v['kind_key']=='sauna' for v in venues)} saunas), {len(events)} events -> {dist}  ({total/1024:.0f} KB total)")
    print(f"not published (no verified/partial content): {len(skipped)} venue rows, {len(ev_skipped)} event rows")
    for k, n, c in skipped[:0]: print("  ", k, n, c)
    if bad:
        for b in bad: print("LEAK-CHECK FAIL:", b)
        sys.exit(2)
    print("leak check OK (no owner credentials / third-party-source names / trackers)")

if __name__ == "__main__":
    main()
