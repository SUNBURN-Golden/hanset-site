# -*- coding: utf-8 -*-
"""HANSET bird's-eye plans (2026-10-30, v3): a sauna-goer's notebook map, seen from straight above.
Flat bold zones (yellow warm bath, red hot bath, blue cold bath, brown/orange saunas), shower icons, entrance box,
English handwriting labels (Gaegu, outlined to paths). Only zones/numbers from official info or venue replies
(site/content/venues_en.json, saunas.csv, boxes.csv); unknown values are left out. Positions/sizes are drawn, not measured.
No photos, maps or logos traced. Usage: python3 make_aerial.py [out_dir]"""
import os, sys, math, random, html as _h
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import handdraw as h
from handdraw import text as _text, tw

W, H = 1080, 1350
INK = "#1d1d1d"
C = dict(warm="#f7e64a", hot="#ef2b22", cold="#2a8de8", jac="#bfe8f6", dry="#b97834", wet="#9cc36a", steam="#f08a24",
         scrub="#f29ad9", grey="#8a8a8a", dark="#5b5b5b", tan="#f3dfa6", green="#2fd23a", floor="#ffffff", pool="#56b8e6",
         wood="#e3b77a", mat="#4a4a4a", lawn="#bfe0a0", sky="#2f7fe0")
rnd = random.Random(7)
_cid = [0]

def T(x, y, s, size=30, anchor="middle", color=INK, op=1.0):
    s = s.replace("–", "-").replace("↑", "upstairs").replace("①", "1.").replace("②", "2.").replace("③", "3.")
    return _text(x, y, s, size, anchor, jitter=False, color=color, op=op)

def speck(path_d, x, y, w, hh, n=None):
    """light paint speckle inside a shape (like the marker texture in hand-made maps)"""
    _cid[0] += 1; cid = f"k{_cid[0]}"
    n = n or int(w * hh / 900)
    dots = "".join(f'<circle cx="{x + rnd.random()*w:.0f}" cy="{y + rnd.random()*hh:.0f}" r="{rnd.uniform(.6, 2.2):.1f}"/>' for _ in range(n))
    return f'<clipPath id="{cid}"><path d="{path_d}"/></clipPath><g clip-path="url(#{cid})" fill="#fff" opacity=".35">{dots}</g>'

def zone(x, y, w, hh, color, label="", sub="", size=34, r=0, tc=INK, steam=False):
    size = int(size * 1.25)
    d = f"M{x+r},{y} H{x+w-r} Q{x+w},{y} {x+w},{y+r} V{y+hh-r} Q{x+w},{y+hh} {x+w-r},{y+hh} H{x+r} Q{x},{y+hh} {x},{y+hh-r} V{y+r} Q{x},{y} {x+r},{y} Z"
    s = f'<path d="{d}" fill="{color}" stroke="{INK}" stroke-width="3"/>' + speck(d, x, y, w, hh)
    cy = y + hh / 2 - (size * 0.35 if sub else -size * 0.35)
    if label:
        for i, l in enumerate(label.split("\n")): s += T(x + w/2, cy + i*size*1.05, l, size, color=tc)
    if sub: s += T(x + w/2, cy + size * (0.95 + 1.05*(label.count("\n"))), sub, int(size*.85), color=tc)
    if steam: s += steamy(x + w/2, y + hh*0.25 if not sub else y + 18)
    return s

def circ(cx, cy, rr, color, label="", sub="", size=34, tc=INK):
    size = int(size * 1.15)
    d = f"M{cx-rr},{cy} A{rr},{rr} 0 1,0 {cx+rr},{cy} A{rr},{rr} 0 1,0 {cx-rr},{cy} Z"
    s = f'<path d="{d}" fill="{color}" stroke="{INK}" stroke-width="3"/>' + speck(d, cx-rr, cy-rr, 2*rr, 2*rr)
    if label: s += T(cx, cy + (0 if sub else size*.35), label, size, color=tc)
    if sub: s += T(cx, cy + size*1.1, sub, int(size*.85), color=tc)
    return s

def steamy(x, y):
    return "".join(f'<path d="M{x-16+i*16},{y+22} q-6,-7 0,-14 q6,-7 0,-14" stroke="{INK}" stroke-width="2.2" fill="none"/>' for i in range(3))

def shower(x, y, standing=True):
    s = f'<circle cx="{x}" cy="{y}" r="9" fill="{C["grey"]}" stroke="{INK}" stroke-width="1.6"/>'
    if standing: s += "".join(f'<path d="M{x-6+i*6},{y+13} l{-2+i*2},10" stroke="{C["sky"]}" stroke-width="1.6"/>' for i in range(3))
    return s

def shower_row(x, y, n, gap=40, standing=True, wall="h"):
    s = f'<rect x="{x-14}" y="{y-24}" width="{gap*(n-1)+28}" height="9" fill="{C["dark"]}"/>' if wall == "h" else ""
    return s + "".join(shower(x + i*gap, y, standing) for i in range(n))

def pillar(x, y, sz=26): return f'<rect x="{x}" y="{y}" width="{sz}" height="{sz}" fill="{C["grey"]}" stroke="{INK}" stroke-width="2"/>'

def entrance(x, y, w=150, hh=70, label="IN / OUT"):
    return zone(x, y, w, hh, C["green"], label, size=32)

def onsen(x, y, s=1.0):
    e = f'<ellipse cx="{x}" cy="{y+16*s}" rx="{30*s}" ry="{11*s}" fill="none" stroke="#e8463c" stroke-width="{4*s}"/>'
    return e + "".join(f'<path d="M{x-14*s+i*14*s},{y+8*s} q-7,-9 0,-18 q7,-9 0,-18" stroke="#e8463c" stroke-width="{4*s}" fill="none"/>' for i in range(3))

def frame(): return f'<rect x="12" y="12" width="{W-24}" height="{H-24}" fill="#fff" stroke="{INK}" stroke-width="14"/>'

def title(x, y, t, size=54):
    sz = size
    while tw(t, sz) > W - x - 60 and sz > 30: sz -= 2
    return onsen(x - 44, y - 34) + T(x, y, t, sz, "start", C["sky"])

def notes(x, y, lines, size=27, lh=1.32, color=INK):
    return "".join(T(x, y + i*size*lh, l, size, "start", color) for i, l in enumerate(lines))

def arrow(x1, y1, x2, y2):
    a = math.atan2(y2-y1, x2-x1)
    hx = lambda d: (x2 - 12*math.cos(a+d), y2 - 12*math.sin(a+d))
    p1, p2 = hx(.45), hx(-.45)
    return f'<path d="M{x1},{y1} Q{(x1+x2)/2+10},{(y1+y2)/2-14} {x2},{y2} M{p1[0]:.0f},{p1[1]:.0f} L{x2},{y2} L{p2[0]:.0f},{p2[1]:.0f}" stroke="{INK}" stroke-width="2.4" fill="none"/>'

def wall(x1, y1, x2, y2, w=9): return f'<path d="M{x1},{y1} L{x2},{y2}" stroke="{C["dark"]}" stroke-width="{w}"/>'

def svgdoc(body, t, d):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img">'
            f'<title>{_h.escape(t)}</title><desc>{_h.escape(d)}</desc>{body}</svg>')

# ------------------------------------------------------------------ venues
def haesurang():
    b = frame()
    b += title(110, 92, "Spa Haesurang, bird's-eye")
    b += notes(70, 150, ["Busan · seawater spa · sauna floor"], 26, color=C["dark"])
    b += notes(640, 150, ["checked 2026.10.18"], 26, color=C["dark"])
    # showers: right wall rows
    for r in range(4): b += shower_row(700, 250 + r*110, 8, 40)
    b += T(840, 700, "~80 shower spots", 30) + T(840, 735, "(official)", 24, color=C["dark"])
    b += zone(60, 210, 300, 210, C["dry"], "Dry sauna", steam=True)
    b += zone(60, 440, 300, 190, C["wet"], "Wet sauna", steam=True)
    b += circ(500, 360, 115, C["warm"], "Warm pool")
    b += zone(400, 500, 250, 170, C["hot"], "Hot pool", tc="#fff")
    b += zone(60, 660, 330, 220, C["cold"], "Cold pool", tc="#fff")
    b += zone(420, 700, 230, 180, C["jac"], "Event bath")
    b += pillar(372, 470) + pillar(372, 640)
    b += zone(680, 770, 330, 110, C["tan"], "Powder room", size=30)
    b += entrance(60, 905, 160, 70)
    b += arrow(230, 945, 300, 945) + T(395, 955, "lockers 4–5F", 28)
    b += notes(70, 1040, ["7F open-air bath"], 30)
    return b

def heosimcheong():
    b = frame()
    b += title(110, 92, "Hurshimchung, bird's-eye")
    b += notes(70, 150, ["Busan · Hotel Nongshim · 5F hot-spring baths", "up to 3,000 people at once (official)"], 26, color=C["dark"])
    b += circ(330, 400, 140, C["warm"], "Longevity", "bath")
    b += circ(560, 330, 80, C["jac"], "Event", "bath", 28)
    b += zone(650, 250, 170, 130, "#7fc4b6", "Celadon", "bath", 28)
    b += zone(850, 230, 170, 260, C["lawn"], "Open-air", "bath", 30)
    b += zone(70, 580, 220, 150, "#9a8f84", "Cave bath", tc="#fff", size=30, r=40)
    b += zone(310, 600, 180, 130, C["cold"], "Cold bath", tc="#fff", size=30)
    b += zone(510, 600, 170, 130, C["warm"], "Half-body", "bath", 28)
    b += zone(700, 520, 150, 110, "#f5f5f5", "Salt bath", size=28)
    b += zone(700, 650, 150, 110, C["pool"], "Mini pool", size=28)
    b += zone(870, 520, 150, 120, C["steam"], "Steam", "sauna", 28, steam=False)
    b += zone(870, 660, 150, 120, C["dry"], "Dry sauna", size=28)
    b += pillar(300, 560) + pillar(680, 560)
    for r in range(2): b += shower_row(110 + r*0, 800 + r*90, 12, 44)
    b += zone(680, 800, 340, 120, C["scrub"], "Scrub corner", size=30)
    b += entrance(70, 960, 250, 70, "IN (4F desk)")
    b += notes(70, 1090, ["Jjimjil (3F)"], 30)
    b += notes(70, 1230, ["checked 2026.10.18"], 24, color=C["dark"])
    return b

def orakai():
    b = frame()
    b += title(110, 92, "Orakai Cheonggyesan, bird's-eye")
    b += notes(70, 150, ["Seoul · hotel sauna · B1"], 26, color=C["dark"])
    b += zone(60, 220, 280, 260, C["dry"], "Dry sauna", steam=True)
    b += zone(60, 500, 280, 230, C["wet"], "Wet sauna", steam=True)
    b += zone(400, 260, 330, 260, C["pool"], "Pool", tc="#fff", r=30)
    b += circ(560, 640, 100, C["jac"], "Pool")
    for r in range(2): b += shower_row(790, 250 + r*120, 6, 40)
    b += T(890, 470, "showers", 28, color=C["dark"])
    b += notes(400, 230, ["several pools (official)"], 24, color=C["dark"])
    b += pillar(370, 560)
    b += entrance(60, 780, 170, 70)
    b += arrow(240, 815, 330, 815) + T(470, 825, "B2: EV charging", 28)
    b += notes(70, 960, ["Men's and women's sides are separate"], 30)
    b += notes(70, 1230, ["checked 2026.10.18"], 24, color=C["dark"])
    return b

def sisu():
    b = frame()
    b += title(110, 92, "Sisu House, bird's-eye")
    b += notes(70, 150, ["Seoul · Sinsa-dong · private sauna for one"], 26, color=C["dark"])
    # private room (2-3F) big
    b += f'<rect x="60" y="200" width="960" height="470" fill="#fff" stroke="{C["dark"]}" stroke-width="8"/>'
    b += T(80, 240, "2–3F private room (one person)", 30, "start")
    b += zone(90, 270, 330, 360, C["dry"], "Finnish", "sauna", steam=True)
    b += zone(450, 300, 260, 160, C["warm"], "Warm bath", "+ bath salts", 30, r=60)
    b += zone(450, 490, 260, 140, C["cold"], "Cold shower", "14°C", 30, tc="#fff")
    b += shower(580, 525, True) if False else ""
    b += circ(850, 360, 70, C["jac"], "Ice", "fountain", 26)
    b += zone(740, 470, 250, 160, C["tan"], "Changing", "room", 28)
    b += zone(60, 700, 470, 200, "#f6d6c8", "1.5F Touch care", "ritual first", 30)
    b += zone(560, 700, 460, 200, C["tan"], "1F Check-in", "+ shop", 30)
    b += entrance(840, 910, 170, 60)
    b += notes(70, 1000, ["① touch care  →  ② warm bath  →  ③ sauna + 14°C cold shower"], 28)
    b += notes(70, 1060, ["Booking only · one person per room"], 30)
    return b

def termeden():
    b = frame()
    b += title(110, 92, "Termeden, bird's-eye")
    b += notes(70, 150, ["Icheon · pool & spa resort · four zones"], 26, color=C["dark"])
    b += f'<rect x="60" y="190" width="960" height="770" fill="{C["lawn"]}" stroke="{INK}" stroke-width="3"/>'
    for i in range(14): b += f'<circle cx="{80 + i*70}" cy="205" r="14" fill="#5f9e4a" stroke="{INK}" stroke-width="1.5"/>'
    # outdoor
    b += zone(90, 240, 380, 170, C["pool"], "Infinity pool", tc="#fff", r=60)
    b += f'<path d="M520,250 C600,300 520,360 610,410 S560,500 650,520" stroke="{C["pool"]}" stroke-width="44" fill="none" stroke-linecap="round"/>'
    b += T(700, 300, "Forest bath", 30, "start")
    b += zone(760, 330, 230, 150, C["pool"], "Mountain pool", tc="#fff", size=28, r=40)
    b += circ(720, 560, 46, C["warm"]) + circ(830, 560, 46, C["hot"]) + T(930, 570, "Terme", 28)
    b += T(270, 450, "OUTDOOR POOL & SPA", 26, color=C["dark"])
    # buildings
    b += f'<rect x="90" y="620" width="900" height="310" fill="#fff" stroke="{C["dark"]}" stroke-width="8"/>'
    b += zone(110, 640, 270, 270, C["pool"], "Indoor", "pool & spa", 30, tc="#fff")
    b += zone(400, 640, 280, 160, C["warm"], "Hot-spring", "bathhouse", 30)
    b += zone(400, 810, 135, 100, C["cold"], "Cold", tc="#fff", size=28) + zone(545, 810, 135, 100, C["dry"], "Saunas", size=26)
    b += zone(700, 640, 270, 270, C["steam"], "Jjimjil spa", "cypress · salt · clay", 28)
    b += arrow(540, 640, 560, 520) + T(470, 590, "open-air bath", 26)
    b += entrance(440, 970, 200, 66, "IN / tickets")
    b += notes(70, 1230, ["checked 2026.10.23"], 24, color=C["dark"])
    return b

def supersapiens():
    b = frame()
    b += title(110, 92, "CrossFit Super Sapiens, bird's-eye")
    b += notes(70, 150, ["Seoul · Gangdong · 3-storey warehouse box"], 26, color=C["dark"])
    b += zone(60, 200, 960, 470, C["mat"], "", "")
    b += T(540, 250, "1F main training floor", 36, color="#fff")
    for i in range(6): b += f'<rect x="{120+i*140}" y="300" width="14" height="150" fill="#d9d9d9"/>'
    b += f'<rect x="110" y="300" width="730" height="12" fill="#d9d9d9"/>' + T(480, 480, "pull-up rig", 26, color="#fff")
    for i in range(5): b += f'<rect x="{150+i*150}" y="540" width="110" height="10" fill="#e6e6e6"/><circle cx="{150+i*150}" cy="545" r="20" fill="#bbb"/><circle cx="{260+i*150}" cy="545" r="20" fill="#bbb"/>'
    b += T(540, 620, "barbells", 26, color="#fff")
    b += zone(60, 700, 470, 240, C["tan"], "2F accessory area", "weights · stretching", 28)
    b += zone(560, 700, 220, 150, C["jac"], "2F", "showers", 28)
    b += shower_row(610, 890, 4, 40)
    b += zone(800, 700, 220, 240, C["wood"], "2F lockers", "", 28)
    b += zone(60, 960, 470, 110, "#f4c26b", "3F tanning", "", 30)
    b += zone(560, 960, 460, 110, "#ddd", "3F staff only", "", 30)
    b += entrance(60, 1090, 170, 66)
    b += notes(260, 1130, ["parking: front yard + church lot next door"], 26, color=C["dark"])
    b += notes(780, 1310, ["checked 2026.10.16"], 22, color=C["dark"])
    return b

SCENES = {
 "aerial-termeden": (termeden, "Termeden, bird's-eye", "Hand-drawn bird's-eye plan of Termeden in Icheon: outdoor infinity pool, forest bath, mountain pool and terme tubs; indoor pool and spa, hot-spring bathhouse with cold bath and saunas, and the jjimjil spa."),
 "aerial-spa-haesurang": (haesurang, "Spa Haesurang, bird's-eye", "Hand-drawn bird's-eye plan of Spa Haesurang's sauna floor in Busan: dry and wet saunas, warm, hot and cold pools, event bath, rows of showers (about 80), powder room and 7F open-air bath."),
 "aerial-heosimcheong": (heosimcheong, "Hurshimchung, bird's-eye", "Hand-drawn bird's-eye plan of the 5th-floor hot-spring baths at Hurshimchung, Busan: longevity, event, celadon, cave, cold, half-body, salt and open-air baths, mini pool, steam and dry saunas, showers and scrub corner, with jjimjil on 3F."),
 "aerial-orakai-cheonggyesan": (orakai, "Orakai Cheonggyesan, bird's-eye", "Hand-drawn bird's-eye plan of the basement sauna at Orakai Cheonggyesan Hotel, Seoul: dry and wet saunas, several pools and showers."),
 "aerial-sisu-house": (sisu, "Sisu House, bird's-eye", "Hand-drawn bird's-eye plan of Sisu House in Seoul: a one-person private room with Finnish sauna, warm bath, 14°C cold shower, ice fountain and changing room, plus the touch-care room and check-in."),
 "aerial-crossfit-super-sapiens": (supersapiens, "CrossFit Super Sapiens, bird's-eye", "Hand-drawn bird's-eye plan of CrossFit Super Sapiens in Seoul: 1st-floor training floor with rig and barbells, 2nd-floor accessory area, showers and lockers, 3rd-floor tanning and staff room."),
}

if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "svg")
    for k, (fn, t, d) in SCENES.items():
        rnd.seed(7); _cid[0] = 0
        open(f"{out}/{k}.svg", "w").write(svgdoc(fn(), t, d)); print(k)
