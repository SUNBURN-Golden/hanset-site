"""Verified feeds: data/verified/events.json + creators.json -> validated items for the builder.

Written by the HANSET operations bot (official sources only). The builder never crashes on these
files: missing, empty or invalid file = no items = section hidden. Invalid items are dropped with a warning.
Rules: https URLs only, required fields, phone/email-like strings rejected, past events dropped, sorted by date.
"""
import datetime as dt, json, re
from pathlib import Path
from urllib.parse import urlparse

EVENT_CATS = ("hyrox", "crossfit", "run", "sauna", "yoga", "other")
PLATFORMS = ("instagram", "youtube", "other")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")


def _date(v):
    if not isinstance(v, str) or not DATE.match(v): return None
    try: return dt.date.fromisoformat(v)
    except ValueError: return None


def _https(v):
    if not isinstance(v, str) or EMAIL.search(v): return None
    u = urlparse(v.strip())
    return v.strip() if u.scheme == "https" and u.netloc and " " not in v else None


def _text(v, maxlen=120, required=True):
    """Plain short text. Rejects phone/email-like content (returns None) so it is never published."""
    if v is None or v == "": return "" if not required else None
    if not isinstance(v, str): return None
    v = " ".join(v.split())
    if not v or len(v) > maxlen or EMAIL.search(v) or PHONE.search(v): return None
    return v


def validate_event(x, today, allow_example=False):
    """Return (item, None) or (None, reason). Past events return (None, 'past')."""
    if not isinstance(x, dict): return None, "not an object"
    if x.get("example") is True and not allow_example: return None, "example"
    out = {}
    for k in ("title", "city", "venue"):
        out[k] = _text(x.get(k))
        if out[k] is None: return None, f"bad {k}"
    pn = _text(x.get("price_note"), 80, required=False)
    if pn is None: return None, "bad price_note"
    out["price_note"] = pn
    if not isinstance(x.get("id"), str) or not SLUG.match(x["id"]): return None, "bad id"
    out["id"] = x["id"]
    if x.get("category") not in EVENT_CATS: return None, "bad category"
    out["category"] = x["category"]
    for k in ("official_url", "source_url"):
        out[k] = _https(x.get(k))
        if not out[k]: return None, f"bad {k}"
    s, v = _date(x.get("start")), _date(x.get("verified_on"))
    if not s or not v: return None, "bad date"
    e = x.get("end")
    e = s if e in (None, "", "null") else _date(e)
    if not e or e < s: return None, "bad end"
    if e < today: return None, "past"
    out.update(start=s, end=e, verified_on=v)
    return out, None


def validate_creator(x, allow_example=False):
    if not isinstance(x, dict): return None, "not an object"
    if x.get("example") is True and not allow_example: return None, "example"
    out = {}
    if not isinstance(x.get("id"), str) or not SLUG.match(x["id"]): return None, "bad id"
    out["id"] = x["id"]
    out["display_name"] = _text(x.get("display_name"), 60)
    if not out["display_name"]: return None, "bad display_name"
    h = x.get("handle")
    if not isinstance(h, str) or not re.match(r"^@[A-Za-z0-9._]{1,40}$", h): return None, "bad handle"
    out["handle"] = h
    if x.get("platform") not in PLATFORMS: return None, "bad platform"
    out["platform"] = x["platform"]
    out["profile_url"] = _https(x.get("profile_url"))
    if not out["profile_url"]: return None, "bad profile_url"
    f = x.get("focus")
    if not isinstance(f, list) or not f or not all(isinstance(t, str) and re.match(r"^[a-z0-9-]{1,24}$", t) for t in f): return None, "bad focus"
    out["focus"] = f[:3]
    c = _text(x.get("city"), 40, required=False)
    if c is None: return None, "bad city"
    out["city"] = c
    out["verified_on"] = _date(x.get("verified_on"))
    if not out["verified_on"]: return None, "bad verified_on"
    return out, None


def _read(path, key):
    p = Path(path)
    if not p.exists(): return []
    try:
        d = json.loads(p.read_text(encoding="utf-8") or "{}")
    except (ValueError, OSError) as e:
        print(f"WARN: {p.name} unreadable ({e.__class__.__name__}); section hidden")
        return []
    if not isinstance(d, dict) or d.get("version") != 1 or not isinstance(d.get(key), list): return []
    return d[key]


def load_events(path, today, allow_example=False):
    out = []
    for x in _read(path, "events"):
        it, why = validate_event(x, today, allow_example)
        if it: out.append(it)
        elif why not in ("past", "example"): print(f"WARN: verified event dropped ({why}): {str(x.get('id') if isinstance(x, dict) else '?')[:40]}")
    out.sort(key=lambda e: (e["start"], e["title"]))
    return out


def load_creators(path, allow_example=False):
    out, seen = [], set()
    for x in _read(path, "creators"):
        it, why = validate_creator(x, allow_example)
        if it and it["id"] not in seen: seen.add(it["id"]); out.append(it)
        elif why and why != "example": print(f"WARN: verified creator dropped ({why}): {str(x.get('id') if isinstance(x, dict) else '?')[:40]}")
    out.sort(key=lambda c: c["display_name"].lower())
    return out
