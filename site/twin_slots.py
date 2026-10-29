"""Digital-twin ontology export -> venue-page slots (English, glanceable).

Display rules (owner, 2026-10-29):
  fact          -> value
  source_claim  -> value + "Source claim, not yet officially confirmed"
  unverified / ai_inference / null value -> "Unverified", never a value
"""
import html, json, re
from pathlib import Path
from urllib.parse import urlsplit

E = html.escape
CLAIM = "Source claim, not yet officially confirmed"
LABELS = {
    ("hours", "regular", "open_hours"): "Hours", ("hours", "closure", "closure_rule"): "Closed",
    ("price_plan", "day_pass", "amount"): "Day pass", ("price_plan", "guest_rate", "amount"): "Guest rate",
    ("price_plan", "listed_price", "amount"): "Listed price",
    ("facility", "dry_sauna", "exists"): "Dry sauna", ("facility", "wet_sauna", "exists"): "Wet sauna",
    ("facility", "sauna", "temperature_c"): "Sauna °C", ("facility", "hot_bath", "exists"): "Hot bath",
    ("facility", "warm_bath", "exists"): "Warm bath", ("facility", "cold_plunge", "exists"): "Cold plunge",
    ("facility", "cold_plunge", "temperature_c"): "Cold plunge °C", ("facility", "rapid_cold_plunge", "exists"): "Rapid cold plunge",
    ("facility", "layout", "bath_list"): "Baths", ("usage_condition", "tattoo", "policy"): "Tattoos",
    ("usage_condition", "foreigner", "policy"): "Foreign visitors", ("usage_condition", "access", "policy"): "Access",
    ("usage_condition", "age", "min_age"): "Min. age", ("program", "program", "listing"): "Programs",
}

def load(path):
    p = Path(path)
    if not p.exists(): return {}
    return json.loads(p.read_text(encoding="utf-8"))["venues"]

def fmt(f):
    v = f["value"]
    if isinstance(v, bool): return "Yes" if v else "No"
    if f["attribute"] == "amount" and isinstance(v, (int, float)): return f"₩{v:,.0f}"
    if f["attribute"] == "temperature_c" and isinstance(v, (int, float)): return f"{v:g}°C"
    if v == "24_hours": return "24 hours"
    if isinstance(v, list): return ", ".join(map(str, v))
    return str(v)

def state(f):
    ok = f["status"] in ("fact", "source_claim") and f["value"] is not None and f.get("source_url") \
        and urlsplit(f["source_url"]).scheme == "https"
    return f["status"] if ok else "unverified"

def rows(fields):
    out = []
    for f in fields:
        st = state(f); key = (f["entity_type"], f["kind"], f["attribute"])
        out.append({"label": LABELS.get(key, f["kind"].replace("_", " ").capitalize()), "status": st,
                    "text": fmt(f) if st != "unverified" else None,
                    "source_url": f["source_url"] if st != "unverified" else None,
                    "checked": (f.get("confirmed_at") or "")[:10].replace("-", ".")})
    return out

def render(fields):
    if not fields: return ""
    rs = rows(fields)
    known = [r for r in rs if r["status"] != "unverified"]
    unk = sorted({r["label"] for r in rs if r["status"] == "unverified"})
    li = "".join(
        f'<li class="tw-{r["status"]}"><span class="tw-k">{E(r["label"])}</span><b>{E(r["text"])}</b>'
        + (f'<em class="tw-chip">{CLAIM}</em>' if r["status"] == "source_claim" else '')
        + f'<a class="tw-src" href="{E(r["source_url"])}" rel="noopener noreferrer">{E(urlsplit(r["source_url"]).hostname or "")}</a><time>{E(r["checked"])}</time></li>'
        for r in known)
    chips = "".join(f'<li><span class="tw-k">{E(l)}</span><em class="tw-chip tw-unv">Unverified</em></li>' for l in unk)
    return (f'<section class="twin" aria-label="Checked details"><h2>{len(known)}<small> checked</small> · {len(unk)}<small> unverified</small></h2>'
            + (f'<ul class="tw-list">{li}</ul>' if li else '')
            + (f'<details class="tw-unk"><summary>Unverified · {len(unk)}</summary><ul class="tw-list">{chips}</ul></details>' if chips else '')
            + '</section>')

def counts(venues):
    c = {"fact": 0, "source_claim": 0, "unverified": 0}
    for fields in venues.values():
        for r in rows(fields): c[r["status"]] += 1
    return c

def check(dist, raw_export_path, venues):
    """Fail if any value that is not fact/source_claim appears on a venue page."""
    bad = []
    raw = json.loads(Path(raw_export_path).read_text(encoding="utf-8")) if raw_export_path and Path(raw_export_path).exists() else None
    for slug, fields in venues.items():
        page = dist / "finder" / slug / "index.html"
        if not page.exists(): continue
        t = page.read_text(encoding="utf-8")
        sec = re.search(r'<section class="twin".*?</section>', t, re.S)
        sec = sec.group(0) if sec else ""
        for f in fields:
            if state(f) == "unverified" and f["value"] is not None and fmt(f) in sec:
                bad.append((slug, f["kind"], "unverified value rendered"))
        if raw:
            for v in raw["venues"]:
                if v.get("public_slug") != slug: continue
                for f in v["fields"]:
                    if f["status"] not in ("fact", "source_claim") and f.get("value") not in (None, "") and str(f["value"]) in sec:
                        bad.append((slug, f["kind"], "non-public value rendered"))
    return bad
