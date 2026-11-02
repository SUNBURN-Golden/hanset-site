# -*- coding: utf-8 -*-
"""HANSET guide drawings (2026-10-30): English notebook plans in the same notebook style as the venue plans.
Generic first-timer guides, no venue numbers. Usage: python3 make_guide.py [out_dir]"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import make_aerial as A  # shared notebook helpers; this file is allowlisted in build_site._aerial_allowed
from make_aerial import C, INK, T, zone, circ, shower_row, entrance, frame, title, notes, arrow, pillar

def step(x, y, n, color=None):
    color = color or A.C["sky"]
    return f'<circle cx="{x}" cy="{y}" r="24" fill="{color}" stroke="{INK}" stroke-width="3"/>' + T(x, y + 11, str(n), 32, color="#fff")

def box_plan():
    b = frame() + title(110, 92, "Inside a CrossFit box")
    b += zone(60, 180, 960, 70, C["tan"], "Whiteboard: today's WOD", size=28)
    b += zone(60, 280, 560, 520, C["mat"], "", "")
    b += T(340, 340, "WOD floor", 44, color="#fff")
    for i in range(4): b += f'<circle cx="{150+i*60}" cy="700" r="18" fill="#bbb" stroke="{INK}" stroke-width="2"/>'
    b += T(240, 760, "kettlebells", 26, color="#fff")
    for i in range(3): b += f'<rect x="{410+i*60}" y="660" width="48" height="48" fill="{C["wood"]}" stroke="{INK}" stroke-width="2"/>'
    b += T(500, 760, "boxes", 26, color="#fff")
    b += zone(650, 280, 370, 170, C["grey"], "Rig", "pull-up bars · rings", 30, tc="#fff")
    b += zone(650, 470, 370, 330, C["dry"], "Open gym", "barbell racks", 30, tc="#fff")
    b += zone(60, 830, 300, 180, C["wood"], "Lockers", "changing", 30)
    b += zone(390, 830, 630, 180, C["jac"], "Rowers · bikes", size=30)
    b += zone(390, 1040, 300, 120, "#f5f5f5", "Front desk", size=28)
    b += entrance(60, 1060, 200, 80, "IN / OUT")
    b += arrow(270, 1100, 380, 1100)
    b += notes(70, 1230, ["Check the timetable before you go"], 28, color=A.C["dark"])
    return b

def dropin_steps():
    b = frame() + title(110, 92, "How a drop-in works")
    items = [("Check", "class times & price online", C["jac"]), ("Book", "or message the box", C["warm"]),
             ("Arrive", "say \"drop-in\" at the desk", C["green"]), ("Train", "warm-up → skill → WOD", C["hot"]),
             ("Wrap up", "put gear back · pay", C["tan"])]
    for i, (t, s, col) in enumerate(items):
        y = 190 + i * 210
        b += step(110, y + 85, i + 1)
        b += zone(170, y, 850, 170, col, t, s, 34, tc="#fff" if col == C["hot"] else INK)
        if i < 4: b += arrow(110, y + 115, 110, y + 250)
    b += notes(70, 1270, ["Bring: water · towel · shoes"], 30)
    return b

def sauna_plan():
    b = frame() + title(110, 92, "How to sauna")
    b += zone(60, 190, 220, 420, C["wood"], "Lockers", size=30)
    b += step(170, 470, 1)
    b += zone(300, 190, 340, 200, C["dry"], "Dry sauna", "dry heat", 30, tc="#fff", steam=True)
    b += zone(660, 190, 360, 200, C["wet"], "Wet sauna", "steam", 30, steam=True)
    b += step(640, 410, 3)
    b += f'<rect x="300" y="410" width="300" height="200" fill="#fff" stroke="{INK}" stroke-width="3"/>'
    b += shower_row(350, 480, 6, 40)
    b += T(450, 570, "Showers", 34)
    b += step(330, 590, 2)
    b += zone(660, 410, 170, 200, C["cold"], "Cold", "plunge", 30, tc="#fff")
    b += step(745, 590, 4)
    b += zone(850, 410, 170, 200, C["tan"], "Rest", "+ water", 30)
    b += step(935, 590, 5)
    b += entrance(60, 630, 220, 70, "IN / OUT")
    seq = ["1. Lockers", "2. Shower first", "3. Sauna, short at first", "4. Cool down (cold is optional)", "5. Rest, drink water", "Repeat or finish"]
    b += notes(70, 790, seq, 34, 1.35)
    b += zone(60, 1120, 960, 150, "#fde9e7", "Skip it if pregnant, unwell,", "after drinking or with heart issues", 28)
    return b

SCENES = {
 "crossfit-box-plan": (box_plan, "Inside a CrossFit box", "Hand-drawn plan of a typical CrossFit box: whiteboard, WOD floor with kettlebells and boxes, rig, open gym with barbell racks, lockers, rowers and bikes, front desk and entrance."),
 "dropin-steps": (dropin_steps, "How a drop-in works", "Hand-drawn drop-in steps: 1 check class times and price online, 2 book or message the box, 3 say drop-in at the desk, 4 train, 5 put gear back and pay."),
 "sauna-guide": (sauna_plan, "How to sauna", "Hand-drawn sauna plan and order: lockers, shower, dry or wet sauna, optional cold plunge, rest and water. Skip it if pregnant, unwell, after drinking or with heart issues."),
}

if __name__ == "__main__":
    outs = sys.argv[1:] or [os.path.join(HERE, "svg"), os.path.join(HERE, "..", "site", "static", "img", "guide")]
    for k, (fn, t, d) in SCENES.items():
        A.rnd.seed(7); A._cid[0] = 0
        doc = A.svgdoc(fn(), t, d)
        for o in outs: open(f"{o}/{k}.svg", "w").write(doc)
        print(k)
