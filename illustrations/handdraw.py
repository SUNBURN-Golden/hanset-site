"""손그림 스타일 SVG 헬퍼 (한셋 HANSET). 글자는 Gaegu Bold(OFL)를 윤곽선(path)으로 변환해 넣으므로
SVG를 <img>·PNG·영상 어디에 써도 폰트 없이 똑같이 보입니다."""
import random, math
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen

INK = "#2e2a27"
PAPER = "#f6efdc"
FONT = TTFont("/workspace/fitness-hub/illustrations/_fonts/Gaegu-Bold.ttf")
GS = FONT.getGlyphSet(); CMAP = FONT.getBestCmap(); UPM = FONT["head"].unitsPerEm
rnd = random.Random(7)
_clip = [0]

def _adv(c, size):
    if c == "·": return size * 0.42
    if c == "→": return size * 1.0
    return GS[CMAP.get(ord(c), CMAP[32])].width * size / UPM

def tw(s, size):
    return sum(_adv(c, size) for c in s)

def text(x, y, s, size=34, anchor="start", rot=0, op=1.0, jitter=True, color=INK):
    w = tw(s, size)
    if anchor == "middle": x -= w / 2
    elif anchor == "end": x -= w
    sc = size / UPM; cx = x; out = []; arrows = []
    for c in s:
        if c == "·":
            r = size * 0.075
            out.append(f"M{cx+size*0.21-r:.1f},{y-size*0.3:.1f} a{r:.1f},{r:.1f} 0 1,0 {2*r:.1f},0 a{r:.1f},{r:.1f} 0 1,0 {-2*r:.1f},0 Z")
            cx += size * 0.42
            continue
        if c == "°":
            r = size * 0.08
            out.append(f"M{cx+size*0.06:.1f},{y-size*0.62:.1f} a{r:.1f},{r:.1f} 0 1,0 {2*r:.1f},0 a{r:.1f},{r:.1f} 0 1,0 {-2*r:.1f},0 Z M{cx+size*0.06+r*0.45:.1f},{y-size*0.62:.1f} a{r*0.55:.1f},{r*0.55:.1f} 0 1,1 {r*1.1:.1f},0 a{r*0.55:.1f},{r*0.55:.1f} 0 1,1 {-r*1.1:.1f},0 Z")
            cx += size * 0.3
            continue
        if c == "→":
            yy = y - size * 0.28; x0 = cx + size * 0.1; x1 = cx + size * 0.9; k = size * 0.2
            out.append(f"M{x0:.1f},{yy:.1f} L{x1:.1f},{yy:.1f} L{x1-k:.1f},{yy-k*0.8:.1f} M{x1:.1f},{yy:.1f} L{x1-k:.1f},{yy+k*0.8:.1f}")
            arrows.append(1)
            cx += size * 1.0
            continue
        g = CMAP.get(ord(c))
        if g is None: g = CMAP[32]
        pen = SVGPathPen(GS, ntos=lambda v: ("%.1f" % v).rstrip("0").rstrip("."))
        dy = rnd.uniform(-1.6, 1.6) if jitter else 0
        tp = TransformPen(pen, (sc, 0, 0, -sc, cx, y + dy))
        GS[g].draw(tp)
        d = pen.getCommands()
        if d: out.append(d)
        cx += GS[g].width * sc
    tr = f' transform="rotate({rot} {x:.1f} {y:.1f})"' if rot else ""
    fills = [d for d in out if not d.startswith("M") or " a" in d or "C" in d or "Q" in d or "Z" in d or "L" not in d or True]
    return f'<path d="{" ".join(out)}" fill="{color}" fill-opacity="{op}" stroke="{color}" stroke-width="{0.0 if not arrows else size*0.06:.1f}" stroke-linecap="round" stroke-linejoin="round"{tr}/>'

def lines_text(x, y, arr, size=32, lh=1.28, anchor="start", op=1.0):
    return "".join(text(x, y + i * size * lh, t, size, anchor, op=op) for i, t in enumerate(arr))

def _wob(a=1.6): return rnd.uniform(-a, a)

def rpath(pts, w=3.6, close=False, op=1.0, passes=2, dash=None, fill="none", jit=1.8):
    """점들을 손으로 그은 듯 흔들리는 곡선으로 잇기(2번 덧긋기)."""
    segs = []
    for p in range(passes):
        q = [(x + _wob(jit), y + _wob(jit)) for x, y in pts]
        d = f"M{q[0][0]:.1f},{q[0][1]:.1f}"
        for i in range(1, len(q)):
            mx = (q[i-1][0] + q[i][0]) / 2 + _wob(jit); my = (q[i-1][1] + q[i][1]) / 2 + _wob(jit)
            d += f" Q{mx:.1f},{my:.1f} {q[i][0]:.1f},{q[i][1]:.1f}"
        if close: d += " Z"
        ww = w if p == 0 else w * 0.62
        o = op if p == 0 else op * 0.75
        da = f' stroke-dasharray="{dash}"' if dash else ""
        f = fill if p == 0 else "none"
        segs.append(f'<path d="{d}" fill="{f}" stroke="{INK}" stroke-width="{ww:.1f}" stroke-opacity="{o}" stroke-linecap="round" stroke-linejoin="round"{da}/>')
    return "".join(segs)

def line(x1, y1, x2, y2, w=3.6, dash=None, op=1.0):
    L = math.hypot(x2-x1, y2-y1) or 1; ux, uy = (x2-x1)/L, (y2-y1)/L
    e = rnd.uniform(0, 5)
    return rpath([(x1-ux*e, y1-uy*e), ((x1+x2)/2, (y1+y2)/2), (x2+ux*e, y2+uy*e)], w, dash=dash, op=op)

def rect(x, y, w, h, lw=3.6, fill=None, dash=None, op=1.0, r=0):
    pts = [(x, y), (x+w/2, y), (x+w, y), (x+w, y+h/2), (x+w, y+h), (x+w/2, y+h), (x, y+h), (x, y+h/2)]
    s = ""
    if fill: s += f'<path d="M{x},{y} h{w} v{h} h{-w} Z" fill="{INK}" fill-opacity="{fill}" stroke="none" transform="translate({_wob(2):.1f},{_wob(2):.1f})"/>'
    # 모서리가 살짝 어긋나게 열린 사각형
    s += rpath(pts, lw, close=True, dash=dash, op=op)
    return s

def ellipse(cx, cy, rx, ry, lw=3.4, fill=None, dash=None):
    n = 14; pts = []
    st = rnd.uniform(0, 6.28)
    for i in range(n + 2):
        a = st + i * 2 * math.pi / n
        k = 1 + rnd.uniform(-0.03, 0.03)
        pts.append((cx + rx*k*math.cos(a), cy + ry*k*math.sin(a)))
    s = ""
    if fill: s += f'<ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" fill="{INK}" fill-opacity="{fill}"/>'
    return s + rpath(pts, lw, dash=dash)

def hatch(x, y, w, h, gap=14, ang=1, lw=1.8, op=0.55):
    _clip[0] += 1; cid = f"c{_clip[0]}"
    s = f'<clipPath id="{cid}"><rect x="{x}" y="{y}" width="{w}" height="{h}"/></clipPath><g clip-path="url(#{cid})">'
    n = int((w + h) / gap) + 2
    for i in range(n):
        k = i * gap
        if ang > 0: s += line(x + k - h, y + h, x + k, y, lw, op=op)
        else: s += line(x + k - h, y, x + k, y + h, lw, op=op)
    return s + "</g>"

def arrow(x1, y1, x2, y2, w=3.4, dash=None, head=16):
    a = math.atan2(y2-y1, x2-x1)
    s = line(x1, y1, x2, y2, w, dash=dash)
    for da in (2.6, -2.6):
        s += rpath([(x2, y2), (x2 + head*math.cos(a+da)*0.9, y2 + head*math.sin(a+da)*0.9)], w, passes=1)
    return s

def curve_arrow(pts, w=3.4, dash="2 11", head=16):
    s = rpath(pts, w, dash=dash, passes=1)
    (x1, y1), (x2, y2) = pts[-2], pts[-1]
    a = math.atan2(y2-y1, x2-x1)
    for da in (2.6, -2.6):
        s += rpath([(x2, y2), (x2 + head*math.cos(a+da)*0.9, y2 + head*math.sin(a+da)*0.9)], w, passes=1)
    return s

def numdot(x, y, n, r=26):
    return ellipse(x, y, r, r, 3.6) + text(x, y + 11, str(n), 34, "middle", jitter=False)

def stick(x, y, s=1.0, pose="stand"):
    """아주 단순한 막대 사람(머리 원 + 몸)."""
    o = ellipse(x, y - 52*s, 11*s, 11*s, 3)
    o += line(x, y - 40*s, x, y - 6*s, 3.4)
    if pose == "stand":
        o += line(x, y - 32*s, x - 18*s, y - 14*s, 3.2) + line(x, y - 32*s, x + 18*s, y - 14*s, 3.2)
        o += line(x, y - 6*s, x - 12*s, y + 26*s, 3.2) + line(x, y - 6*s, x + 12*s, y + 26*s, 3.2)
    elif pose == "up":
        o += line(x, y - 34*s, x - 22*s, y - 62*s, 3.2) + line(x, y - 34*s, x + 22*s, y - 62*s, 3.2)
        o += line(x, y - 6*s, x - 12*s, y + 26*s, 3.2) + line(x, y - 6*s, x + 12*s, y + 26*s, 3.2)
    elif pose == "sit":
        o += line(x, y - 32*s, x - 16*s, y - 12*s, 3.2) + line(x, y - 32*s, x + 16*s, y - 12*s, 3.2)
        o += line(x, y - 6*s, x + 26*s, y - 6*s, 3.2) + line(x + 26*s, y - 6*s, x + 26*s, y + 22*s, 3.2)
    return o

def svg(body, W=1080, H=1350, title="", desc=""):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img">
<title>{title}</title><desc>{desc}</desc>
<defs>
<filter id="paper" x="0" y="0" width="100%" height="100%"><feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2" seed="4" result="n"/><feColorMatrix type="matrix" values="0 0 0 0 0.35  0 0 0 0 0.28  0 0 0 0 0.18  0 0 0 0.10 0"/></filter>
<filter id="rough" x="-2%" y="-2%" width="104%" height="104%"><feTurbulence type="fractalNoise" baseFrequency="0.035" numOctaves="2" seed="9" result="t"/><feDisplacementMap in="SourceGraphic" in2="t" scale="3.2" xChannelSelector="R" yChannelSelector="G"/></filter>
<radialGradient id="vig" cx="50%" cy="50%" r="75%"><stop offset="70%" stop-color="#d9c9a0" stop-opacity="0"/><stop offset="100%" stop-color="#cdb88a" stop-opacity="0.35"/></radialGradient>
</defs>
<rect width="{W}" height="{H}" fill="{PAPER}"/>
<rect width="{W}" height="{H}" filter="url(#paper)"/>
<rect width="{W}" height="{H}" fill="url(#vig)"/>
<g filter="url(#rough)">{body}</g>
</svg>'''

def header(title, sub):
    s = text(60, 118, title, 78, "start", rot=-0.8)
    s += rpath([(60, 142), (300, 148), (560, 141)], 4.5, passes=2)
    s += text(62, 196, sub, 32, "start", op=0.85)
    return s

def footer(lines_, y0=None, H=1350):
    y0 = y0 or H - 105
    s = rpath([(60, y0-34), (540, y0-30), (1020, y0-35)], 2.4, passes=1, dash="3 9", op=0.8)
    s += text(60, y0, lines_[0], 31)
    if len(lines_) > 1: s += text(60, y0 + 40, lines_[1], 28, op=0.85)
    s += text(1020, H - 38, "한셋 HANSET", 30, "end", rot=-1)
    return s
