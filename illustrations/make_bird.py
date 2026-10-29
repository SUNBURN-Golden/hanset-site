# -*- coding: utf-8 -*-
"""HANSET floor-by-floor sketches, 6 venues (English labels, 2026-10-30).
Only floor and zone names published in official guides. Zone positions are schematic; nothing is invented.
No images copied or traced."""
import sys, os, html as _h
sys.path.insert(0, "/workspace/fitness-hub/illustrations")
from handdraw import *
import handdraw as h
from make_archive import wrap, fit, steam, waves, curve_arrow

CHECK = "2026.10.18"
K = 0.46   
SAFE = ["Heart disease, pregnancy, after drinking or feeling unwell? Skip the sauna", "and cold plunge, or ask a doctor first. Not medical advice."]

def P(ox, oy, gx, gy, gz=0):
    return (ox + (gx - gy), oy + (gx + gy) * K - gz)

def poly(pts, fill="none", lw=3.4, dash=None, op=1.0, passes=2, jit=1.4):
    return rpath(pts, lw, close=True, fill=fill, dash=dash, op=op, passes=passes, jit=jit)

def hatch_poly(pts, gap=11, lw=1.5, op=0.5, ang=1):
    h._clip[0] += 1; cid = f"c{h._clip[0]}"
    d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + " Z"
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    s = f'<clipPath id="{cid}"><path d="{d}"/></clipPath><g clip-path="url(#{cid})">'
    n = int(((x1 - x0) + (y1 - y0)) / gap) + 2
    for i in range(n):
        k = i * gap
        if ang > 0: s += line(x0 + k - (y1 - y0), y1, x0 + k, y0, lw, op=op)
        else: s += line(x0 + k - (y1 - y0), y0, x0 + k, y1, lw, op=op)
    return s + "</g>"

def slab(ox, oy, S, t, ghost=False, walls=0):
    """helper"""
    T = P(ox, oy, 0, 0); R = P(ox, oy, S, 0); B = P(ox, oy, S, S); L = P(ox, oy, 0, S)
    
    s = ""
    dn = lambda p: (p[0], p[1] + t)
    if ghost:
        s += poly([T, R, B, L], dash="12 9", lw=3.2, passes=1)
        return s
    s += poly([L, B, dn(B), dn(L)], fill=PAPER, lw=3.4)
    s += hatch_poly([L, B, dn(B), dn(L)], gap=9, lw=1.3, op=0.45, ang=-1)
    s += poly([B, R, dn(R), dn(B)], fill=PAPER, lw=3.4)
    s += f'<path d="M{B[0]:.1f},{B[1]:.1f} L{R[0]:.1f},{R[1]:.1f} L{dn(R)[0]:.1f},{dn(R)[1]:.1f} L{dn(B)[0]:.1f},{dn(B)[1]:.1f} Z" fill="{INK}" fill-opacity="0.22"/>'
    s += poly([T, R, B, L], fill=PAPER, lw=3.6)
    s += f'<path d="M{T[0]:.1f},{T[1]:.1f} L{R[0]:.1f},{R[1]:.1f} L{B[0]:.1f},{B[1]:.1f} L{L[0]:.1f},{L[1]:.1f} Z" fill="{INK}" fill-opacity="0.04"/>'
    if walls:
        
        Tt = (T[0], T[1] - walls); Rt = (R[0], R[1] - walls); Lt = (L[0], L[1] - walls)
        s += poly([L, T, Tt, Lt], fill=PAPER, lw=3.0, passes=1)
        s += hatch_poly([L, T, Tt, Lt], gap=10, lw=1.2, op=0.35, ang=1)
        s += poly([T, R, Rt, Tt], fill=PAPER, lw=3.0, passes=1)
        s += f'<path d="M{T[0]:.1f},{T[1]:.1f} L{R[0]:.1f},{R[1]:.1f} L{Rt[0]:.1f},{Rt[1]:.1f} L{Tt[0]:.1f},{Tt[1]:.1f} Z" fill="{INK}" fill-opacity="0.12"/>'
    return s

def block(ox, oy, gx, gy, side, hgt, motif=None):
    """helper"""
    a = side
    A = P(ox, oy, gx - a/2, gy - a/2, 0); Bp = P(ox, oy, gx + a/2, gy - a/2, 0)
    C = P(ox, oy, gx + a/2, gy + a/2, 0); D = P(ox, oy, gx - a/2, gy + a/2, 0)
    up = lambda p: (p[0], p[1] - hgt)
    s = ""
    s += poly([D, C, up(C), up(D)], fill=PAPER, lw=3.0, passes=1, jit=1.0)
    s += hatch_poly([D, C, up(C), up(D)], gap=8, lw=1.2, op=0.45, ang=-1)
    s += poly([C, Bp, up(Bp), up(C)], fill=PAPER, lw=3.0, passes=1, jit=1.0)
    s += f'<path d="M{C[0]:.1f},{C[1]:.1f} L{Bp[0]:.1f},{Bp[1]:.1f} L{up(Bp)[0]:.1f},{up(Bp)[1]:.1f} L{up(C)[0]:.1f},{up(C)[1]:.1f} Z" fill="{INK}" fill-opacity="0.2"/>'
    s += poly([up(A), up(Bp), up(C), up(D)], fill=PAPER, lw=3.0, passes=1, jit=1.0)
    cx, cy = P(ox, oy, gx, gy, hgt)
    if motif == "steam": s += steam(cx - 28, cy - 62, 3)
    elif motif == "wave": s += waves(cx - 26, cy - 8, 52, 2, 14)
    elif motif == "locker":
        for i in range(4): s += rect(cx - 28 + i * 15, cy - 26, 12, 22, 2.4, fill=0.12)
    elif motif == "bar":
        s += line(cx - 38, cy - 20, cx + 38, cy - 20, 4.4) + rect(cx - 46, cy - 36, 10, 32, 2.8, fill=0.5) + rect(cx + 36, cy - 36, 10, 32, 2.8, fill=0.5)
    elif motif == "sun":
        s += ellipse(cx, cy - 24, 12, 12, 3) + "".join(line(cx + 20*math.cos(a_), cy - 24 + 20*math.sin(a_), cx + 30*math.cos(a_), cy - 24 + 30*math.sin(a_), 2.6) for a_ in [i*math.pi/4 for i in range(8)])
    elif motif == "car":
        s += rect(cx - 24, cy - 28, 48, 18, 3, fill=0.1) + ellipse(cx - 14, cy - 8, 6, 6, 2.6) + ellipse(cx + 14, cy - 8, 6, 6, 2.6)
    elif motif == "desk":
        s += rect(cx - 26, cy - 22, 52, 14, 3, fill=0.1)
    return s

def floor_label(lx, ty, tag, title, items, size=25, wlab=450, dashed=False):
    """helper"""
    s = ellipse(lx + 30, ty + 26, 30, 30, 3.6, dash=("4 6" if dashed else None))
    s += text(lx + 30, ty + 26 + 11, tag, fit(tag, 32, 54), "middle", jitter=False)
    s += text(lx + 76, ty + 38, title, fit(title, 38, wlab - 76))
    yy = ty + 38 + size * 1.3; n = 0
    for it in items:
        for i, l in enumerate(wrap("· " + it, size, wlab - 76)):
            s += text(lx + 76 + (size * 0.9 if i > 0 else 0), yy, l, size); yy += size * 1.22; n += 1
    return s, (yy - ty)

def label_height(title, items, size=25, wlab=450):
    n = sum(len(wrap("· " + it, size, wlab - 76)) for it in items)
    return 38 + size * 1.3 + n * size * 1.22 - size * 0.4 + 10

def tower(floors, cx, S, t=18, top=238, bottom=1066, lx=540, wlab=500, size=25, pmax_ratio=1.0):
    """helper"""
    n = len(floors)
    hh = 2 * S * K
    avail = bottom - top
    p = (avail - hh - t) / max(1, n - 1) if n > 1 else 0
    p = min(p, hh * pmax_ratio)
    used = p * (n - 1) + hh + t
    y0 = top + max(0, (avail - used) / 2)
    ys = [y0 + i * p for i in range(n)]
    labh = [label_height(f["title"], f.get("items", []), size, wlab) for f in floors]
    
    ideal = [ys[i] + hh / 2 + floors[i].get("tt", t) / 2 - 26 for i in range(n)]
    tops = []; cur = -1e9
    for i in range(n):
        ty = max(ideal[i], cur, top - 6); tops.append(ty); cur = ty + labh[i] + 2
    over = tops[-1] + labh[-1] - bottom
    if over > 0:
        
        nxt = bottom
        for i in range(n - 1, -1, -1):
            tops[i] = min(tops[i], nxt - labh[i]); nxt = tops[i] - 2
    body = ""; labels = ""
    for i in range(n)[::-1]:
        f = floors[i]; ox = cx; oy = ys[i]; tt = f.get("tt", t)
        body += slab(ox, oy, S, tt, ghost=f.get("ghost", False))
        bl = f.get("blocks", []); m = len(bl)
        if m:
            if m == 1: pos = [(0.62, 0.62)]
            elif m == 2: pos = [(0.34, 0.76), (0.76, 0.34)] if i != 0 else [(0.34, 0.66), (0.70, 0.30)]
            elif i == 0: pos = [(0.30, 0.72), (0.72, 0.30), (0.36, 0.34), (0.72, 0.72)]
            else: pos = [(0.36, 0.76), (0.76, 0.36), (0.80, 0.80), (0.5, 0.98)]
            ps = sorted(zip(pos[:m], bl), key=lambda q: q[0][0] + q[0][1])
            for (px_, py_), (mot, bh) in ps:
                body += block(ox, oy, px_ * S, py_ * S, 0.30 * S * (1.0 if m <= 3 else 0.9), bh, mot)
        ly = tops[i]
        rp = P(ox, oy, S, 0)
        rpx, rpy = rp[0] + 8, rp[1] + tt / 2
        sl, hl = floor_label(lx, ly, f["tag"], f["title"], f.get("items", []), size, wlab, dashed=f.get("ghost", False))
        labels += sl
        labels += rpath([(rpx, rpy), ((rpx + lx) / 2, (rpy + ly + 26) / 2 + 4), (lx - 8, ly + 26)], 2.6, passes=1, dash="2 9", op=0.85)
    guides = ""
    for i in range(n - 1):
        tt = floors[i].get("tt", t)
        for gx_, gy_ in ((0, S), (S, 0)):
            a_ = P(cx, ys[i], gx_, gy_); b_ = P(cx, ys[i + 1], gx_, gy_)
            guides += line(a_[0], a_[1] + tt + 2, b_[0], b_[1] - 2, 2.4, dash="2 9", op=0.8)
    return body + guides + labels, S

def base(title, sub):
    sz = fit(title, 76, 960)
    s = text(60, 118, title, sz, "start", rot=-0.8)
    s += rpath([(60, 142), (300, 148), (min(60 + tw(title, sz), 900), 141)], 4.5, passes=2)
    s += text(62, 196, sub, fit(sub, 30, 450), "start", op=0.85)
    return s

def foot(src, sauna=True, extra=None, y=1092):
    s = rpath([(60, y), (540, y + 4), (1020, y - 1)], 2.4, passes=1, dash="3 9", op=0.8)
    l3 = f"Source {src} · checked {CHECK}"
    s += text(60, y + 42, l3, fit(l3, 27, 960), op=0.9)
    yy = y + 76
    for e in (extra or []):
        s += text(60, yy, e, fit(e, 25, 960), op=0.85); yy += 29
    if sauna:
        for e in SAFE:
            s += text(60, yy, e, fit(e, 24, 960), op=0.85); yy += 28
    s += text(1020, 1334, "HANSET", 30, "end", rot=-1)
    return s

# ------------------------------------------------------------------ scenes
def haesurang():
    b = base("Spa Haesurang, floor by floor", "Busan · official amenities page")
    fl = [
        dict(tag="7F", title="Open-air bath", items=["Open-air bath · Finnish sauna", "Lounge bar · big screen", "PDR rooms (Blue Wave · Red Harmony · Purple Sky)"], blocks=[("wave", 24), ("steam", 30), ("plain", 22)]),
        dict(tag="6F", title="Not listed officially", items=[], ghost=True),
        dict(tag="5F", title="Jjimjil rooms", items=["Relax · massage chair · cave · photo · claw machine · kids zones", "Snack bar · food court", "Sauna lockers on 4-5F (official)"], blocks=[("steam", 26), ("plain", 20), ("desk", 18)]),
        dict(tag="4F", title="Lockers · powder room", items=["Sauna lockers 4-5F · powder rooms 3-4F (official)"], blocks=[("locker", 30), ("plain", 20)]),
        dict(tag="3F", title="Powder room", items=["Cotton swabs · combs · hair dryers (official)"], blocks=[("desk", 20), ("plain", 18)]),
        dict(tag="2F", title="Lobby", items=["Kiosks · shoe lockers · info desk"], blocks=[("desk", 22), ("plain", 20)]),
    ]
    sv, S = tower(fl, 272, 182, top=240, bottom=1062)
    b += sv
    b += foot("spahaesurang.co.kr", True, ["Sauna (official): wet and dry saunas, warm and hot pools, cold pool, event bath, about 80 shower spots"])
    return b

from make_archive import steps_box, side_note

def sisu():
    b = base("Sisu House, floor by floor", "Seoul · official space guide")
    fl = [
        dict(tag="2-3F", title="Private Zone", tt=40, items=["Private Changing Room", "Warm Bath · Cold Shower", "Finnish Sauna · Ice Fountain"], blocks=[("steam", 30), ("wave", 22), ("plain", 20)]),
        dict(tag="1.5F", title="Touch Zone", items=["Pre-Sauna Touch Care"], blocks=[("desk", 20), ("plain", 18)]),
        dict(tag="1F", title="Welcoming Zone", items=["Check In", "Shop"], blocks=[("desk", 22), ("plain", 20)]),
    ]
    sv, S = tower(fl, 270, 186, top=236, bottom=772, t=24)
    b += sv
    st, ye = steps_box(60, 788, 480, "Ritual steps (official)", ["Warm-up · touch care", "Totonoi · sauna in the private zone", "Cold Shower & Cold Bath", "Clarity · wind down"], size=26)
    b += st
    sn, y2 = side_note(600, 788, 420, "How it works (official)", ["100% advance booking (Naver Booking)", "One person per room", "Only the person who booked"], size=25)
    b += sn
    b += foot("sisu.house", True, y=1098)
    return b

def orakai():
    b = base("Orakai Cheonggyesan, floor by floor", "Seoul · hotel B1 sauna")
    fl = [
        dict(tag="B1", title="Sauna (official: B1)", items=["Dry sauna · wet sauna", "Several pools", "Body scrub: walk-in sign-up only", "'LEVEL B1' restaurant on the same floor"], blocks=[("steam", 30), ("wave", 22), ("plain", 20)]),
        dict(tag="B2", title="Parking · EV charging", items=["EV chargers on B2 (official guide)"], blocks=[("car", 20), ("plain", 16)]),
    ]
    sv, S = tower(fl, 270, 200, top=246, bottom=790, t=28)
    b += sv
    sn, y2 = side_note(60, 830, 470, "Who can use (official)", ["Open to non-guests too", "Under 8 not admitted", "Ages 8-16 with a guardian"], size=25)
    b += sn
    sn2, y3 = side_note(580, 830, 440, "Good to know", ["Has regular closing days", "Prices and hours: see the venue page"], size=25)
    b += sn2
    b += foot("cgs.orakaihotels.com", True, y=1098)
    return b

def heosimcheong():
    b = base("Hurshimchung, floor by floor", "Busan · Hotel Nongshim")
    fl = [
        dict(tag="5F", title="Hot-spring baths", items=["Separate men's and women's baths, 3,000 people at once (official)", "Longevity · event · celadon · open-air · cave · cold · half-body · salt baths · mini pool and more", "Steam sauna (wet) · high-heat sauna (dry) · showers · scrub corner"], blocks=[("wave", 22), ("steam", 28), ("wave", 20)]),
        dict(tag="4F", title="Entry and exit", items=["Front desk (keys · payment)", "Lockers (men · women) · barber · shoe repair", "Get your robe at the locker-room shop"], blocks=[("desk", 22), ("locker", 26)]),
        dict(tag="3F", title="Jjimjil rooms", items=["Charcoal · aroma · clay · gem · ice rooms", "Big rest hall · sleeping room · cafe & snacks · gym"], blocks=[("steam", 26), ("plain", 20)]),
        dict(tag="1-2F", title="Food · banquet", items=["1F bakery · Chinese restaurant · fast food · cafe · brewpub · hall", "2F banquet halls · wedding hall (link to the hotel)"], blocks=[("plain", 22), ("plain", 18)]),
        dict(tag="B1·2", title="Parking", items=["No RVs (use the parking building)"], blocks=[("car", 16)]),
    ]
    sv, S = tower(fl, 262, 168, top=236, bottom=1062, size=23, wlab=520, lx=530, t=16)
    b += sv
    b += foot("hotelnongshim.com", True, ["How it works (official): key at the 4F desk → lockers → 5F baths · 3F jjimjil → pay at 4F when you leave"], y=1092)
    return b

def supersapiens():
    b = base("CrossFit Super Sapiens, floor by floor", "Seoul · 'Our Facility' page")
    fl = [
        dict(tag="3F", title="Extras", items=["Tanning for members", "Staff-only space"], blocks=[("sun", 22), ("plain", 20)]),
        dict(tag="2F", title="Amenities · accessory training", items=["Showers · lockers", "Weights · stretching · cardio zone", "Community space"], blocks=[("locker", 26), ("plain", 20), ("plain", 18)]),
        dict(tag="1F", title="Main training floor", items=["CrossFit equipment", "WOD and open-gym space"], blocks=[("bar", 24), ("plain", 18)]),
    ]
    sv, S = tower(fl, 270, 196, top=236, bottom=820, t=28)
    b += sv
    sn, y2 = side_note(60, 850, 470, "Parking (official)", ["Front yard of the box", "Church parking lot next door"], size=26)
    b += sn
    sn2, y3 = side_note(580, 850, 440, "Good to know", ["Official: a 3-storey warehouse space", "Drop-in and trial booking: see the venue page"], size=24)
    b += sn2
    b += foot("crossfitsupersapiens.com", False, y=1110)
    return b

def termeden():
    b = base("Termeden, zone by zone", "Icheon · official facility guide")
    S = 290; ox, oy = 540, 238
    b += slab(ox, oy, S, 22)
    zones = [
        (0.27, 0.27, "wave", 22, 1, False), (0.73, 0.27, "steam", 24, 2, False),
        (0.27, 0.73, "plain", 20, 3, False), (0.73, 0.73, "wave", 20, 4, True),
    ]
    for gx, gy, mot, bh, num, tier in sorted(zones, key=lambda z: z[0] + z[1]):
        b += block(ox, oy, gx * S, gy * S, 0.33 * S, bh, mot if not tier else "plain")
        top_y = bh
        if tier:
            b += block(ox, oy - bh, gx * S, gy * S, 0.17 * S, 16, "wave")
            top_y = bh + 16
        px, py = P(ox, oy, gx * S, gy * S, top_y)
        b += ellipse(px, py - 26 - (62 if mot == "steam" else 0), 20, 20, 3.4) + text(px, py - 26 - (62 if mot == "steam" else 0) + 10, str(num), 28, "middle", jitter=False)
    fr = P(ox, oy, S, S)
    b += text(540, fr[1] + 52, "Entrance · staffed ticket desk · pay on 2F (official)", 27, "middle")
    cards = [
        (1, "Indoor pool & spa", ["Indoor bade pool (lazy river · kids pool)", "Indoor event bath · aroma sauna (dry)", "Floating · neck showers · relax loungers"]),
        (2, "Hot-spring bathhouse", ["Men's and women's (warm · cold baths · showers)", "Dry and wet saunas · open-air bath · event bath", "Open-air bath joins the bathhouse by a folding door"]),
        (3, "Jjimjil spa", ["Cypress · salt · clay · elvan-stone rooms", "Massage-chair room · kids library", "Family room · garden lounge · jjimjil wear at the locker desk"]),
        (4, "Outdoor pool & spa (1F · 2F)", ["Infinity pool · forest bath · water play", "Mountain pool · terme (pavilion bath · event bath)", "Cabanas, slides and fire shows by season and time"]),
    ]
    ys0 = 640
    for i, (num, ttl, its) in enumerate(cards):
        cx0 = 60 + (i % 2) * 500; cy0 = ys0 + (i // 2) * 215
        b += ellipse(cx0 + 24, cy0 + 22, 22, 22, 3.4) + text(cx0 + 24, cy0 + 33, str(num), 30, "middle", jitter=False)
        b += text(cx0 + 62, cy0 + 36, ttl, fit(ttl, 36, 410))
        yy = cy0 + 36 + 34
        for it in its:
            for j, l in enumerate(wrap("· " + it, 24, 440)):
                b += text(cx0 + 62 + (22 if j > 0 else 0), yy, l, 24); yy += 29
    b += foot("termeden.com", True, ["Closures, clean days and hours: see the notices on the venue page."], y=1076)
    return b

SCENES = {
    "archive-sisu-house": (sisu, "Sisu House, floor by floor", "Hand-drawn floor-by-floor sketch of Sisu House from its official space guide: 1F Welcoming Zone, 1.5F Touch Zone, 2-3F Private Zone, plus the ritual steps."),
    "archive-spa-haesurang": (haesurang, "Spa Haesurang, floor by floor", "Hand-drawn floor-by-floor sketch of Spa Haesurang from its official amenities page: 2F lobby, 3-4F powder rooms, 4-5F lockers, 5F jjimjil, 7F open-air bath; 6F is not listed."),
    "archive-heosimcheong": (heosimcheong, "Hurshimchung, floor by floor", "Hand-drawn floor-by-floor sketch of Hurshimchung at Hotel Nongshim: B1-B2 parking, 1F food, 2F banquet, 3F jjimjil, 4F lockers and desk, 5F hot-spring baths."),
    "archive-termeden": (termeden, "Termeden, zone by zone", "Hand-drawn zone-by-zone sketch of Termeden from its official facility guide: indoor pool & spa, hot-spring bathhouse, jjimjil spa, outdoor pool & spa."),
    "archive-orakai-cheonggyesan": (orakai, "Orakai Cheonggyesan, floor by floor", "Hand-drawn floor-by-floor sketch of the Orakai Cheonggyesan Hotel sauna: B1 dry and wet saunas, several pools and walk-in body scrub; B2 EV charging."),
    "archive-crossfit-super-sapiens": (supersapiens, "CrossFit Super Sapiens, floor by floor", "Hand-drawn floor-by-floor sketch of CrossFit Super Sapiens: 1F main training floor, 2F showers, lockers and accessory training, 3F tanning and staff space."),
}

def render(only=None, outdir="/workspace/fitness-hub/illustrations/svg"):
    for k, (fn, t, d) in SCENES.items():
        if only and k not in only: continue
        h.rnd.seed(11); h._clip[0] = 0
        open(f"{outdir}/{k}.svg", "w").write(svg(fn(), title=_h.escape(t), desc=_h.escape(d)))
        print(k)

if __name__ == "__main__":
    os.makedirs("/workspace/fitness-hub/illustrations/_work", exist_ok=True)
    render(outdir="/workspace/fitness-hub/illustrations/_work")
