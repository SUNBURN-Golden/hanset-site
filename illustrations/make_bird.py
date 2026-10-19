# -*- coding: utf-8 -*-
"""한셋 '조감도(아이소메트릭) 손그림' 6장 (2026-10-18).
공식 안내에 글자로 공개된 층·구역 이름만 사용. 구역의 위치·크기는 개념도(그리지 않은 정보는 지어내지 않음).
원본 이미지 복사·트레이싱 없음. 가격·시간 미표기."""
import sys, os, html as _h
sys.path.insert(0, "/workspace/fitness-hub/illustrations")
from handdraw import *
import handdraw as h
from make_archive import wrap, fit, steam, waves, curve_arrow

CHECK = "2026.10.18"
K = 0.46   # 세로 압축(아이소메트릭 느낌)
SAFE = ["심혈관 질환·임신·음주 후·컨디션 불량 시 사우나·냉수욕을 피하거나", "의사와 상담하세요. 의학적 조언이 아닙니다."]

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
    """ox,oy = 윗면 맨 위 꼭짓점. 정사각 바닥 S×S 슬래브(두께 t). 반환 (svg, 윗면 좌표 함수)"""
    T = P(ox, oy, 0, 0); R = P(ox, oy, S, 0); B = P(ox, oy, S, S); L = P(ox, oy, 0, S)
    # (gx 증가 = 오른쪽 아래, gy 증가 = 왼쪽 아래) → 위 꼭짓점 T, 오른쪽 R, 아래 B, 왼쪽 L
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
        # 뒤쪽 두 벽(낮은 칸막이)
        Tt = (T[0], T[1] - walls); Rt = (R[0], R[1] - walls); Lt = (L[0], L[1] - walls)
        s += poly([L, T, Tt, Lt], fill=PAPER, lw=3.0, passes=1)
        s += hatch_poly([L, T, Tt, Lt], gap=10, lw=1.2, op=0.35, ang=1)
        s += poly([T, R, Rt, Tt], fill=PAPER, lw=3.0, passes=1)
        s += f'<path d="M{T[0]:.1f},{T[1]:.1f} L{R[0]:.1f},{R[1]:.1f} L{Rt[0]:.1f},{Rt[1]:.1f} L{Tt[0]:.1f},{Tt[1]:.1f} Z" fill="{INK}" fill-opacity="0.12"/>'
    return s

def block(ox, oy, gx, gy, side, hgt, motif=None):
    """윗면 좌표 (gx,gy)에 놓는 작은 직육면체(구역). 위치는 개념도."""
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
    """라벨 블록. (svg, 높이)"""
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
    """floors: 위층 → 아래층 순서. dict(tag,title,items,blocks=[(motif,height)],ghost,tt(두께))"""
    n = len(floors)
    hh = 2 * S * K
    avail = bottom - top
    p = (avail - hh - t) / max(1, n - 1) if n > 1 else 0
    p = min(p, hh * pmax_ratio)
    used = p * (n - 1) + hh + t
    y0 = top + max(0, (avail - used) / 2)
    ys = [y0 + i * p for i in range(n)]
    labh = [label_height(f["title"], f.get("items", []), size, wlab) for f in floors]
    # 라벨 y: 이상적 위치(슬래브 옆)에서 시작해 겹치면 아래로 밀고, 넘치면 전체를 위로
    ideal = [ys[i] + hh / 2 + floors[i].get("tt", t) / 2 - 26 for i in range(n)]
    tops = []; cur = -1e9
    for i in range(n):
        ty = max(ideal[i], cur, top - 6); tops.append(ty); cur = ty + labh[i] + 2
    over = tops[-1] + labh[-1] - bottom
    if over > 0:
        # 아래에서부터 위로 당기기
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
    s += text(62, 196, sub, fit(sub, 32, 960), "start", op=0.85)
    return s

def foot(src, sauna=True, extra=None, y=1092):
    s = rpath([(60, y), (540, y + 4), (1020, y - 1)], 2.4, passes=1, dash="3 9", op=0.8)
    l1 = "구역 배치는 개념도예요 · 공식 안내를 바탕으로 한 손그림"
    l2 = "실제와 다를 수 있어요"
    l3 = f"출처 {src} · 확인일 {CHECK}"
    s += text(60, y + 42, l1, fit(l1, 30, 960)) + text(60, y + 78, l2, fit(l2, 30, 960))
    s += text(60, y + 112, l3, fit(l3, 27, 960), op=0.9)
    yy = y + 142
    for e in (extra or []):
        s += text(60, yy, e, fit(e, 25, 960), op=0.85); yy += 29
    if sauna:
        for e in SAFE:
            s += text(60, yy, e, fit(e, 24, 960), op=0.85); yy += 28
    s += text(1020, 1334, "한셋 HANSET", 30, "end", rot=-1)
    return s

# ------------------------------------------------------------------ 장면
def haesurang():
    b = base("스파 해수랑, 조감도", "부산 서구 · 공식 편의시설 층 표기를 쌓아 올린 손그림")
    fl = [
        dict(tag="7F", title="노천탕", items=["노천탕 · 핀란드 사우나", "라운지바 · 대형스크린", "PDR룸(블루 웨이브 · 레드 하모니 · 퍼플 스카이)"], blocks=[("wave", 24), ("steam", 30), ("plain", 22)]),
        dict(tag="6F", title="공식 층 안내 없음", items=[], ghost=True),
        dict(tag="5F", title="찜질방", items=["릴렉스존 · 안마의자존 · 동굴존 · 포토존 · 인형뽑기 · 키즈존", "스낵바 · 푸드코트", "사우나 락커룸은 4~5F(공식 층 표기)"], blocks=[("steam", 26), ("plain", 20), ("desk", 18)]),
        dict(tag="4F", title="락커룸 · 파우더룸", items=["사우나 락커룸 4~5F · 파우더룸 3~4F(공식 층 표기)"], blocks=[("locker", 30), ("plain", 20)]),
        dict(tag="3F", title="파우더룸", items=["면봉 · 빗 · 드라이기가 있는 공간(공식)"], blocks=[("desk", 20), ("plain", 18)]),
        dict(tag="2F", title="로비", items=["키오스크 · 신발장 · 인포데스크"], blocks=[("desk", 22), ("plain", 20)]),
    ]
    sv, S = tower(fl, 272, 182, top=240, bottom=1062)
    b += sv
    b += foot("spahaesurang.co.kr", True, ["사우나 구성(공식): 습식·건식 사우나, 온탕·열탕, 냉탕, 이벤트탕, 샤워 공간(80여 개)"])
    return b

from make_archive import steps_box, side_note

def sisu():
    b = base("시수하우스, 조감도", "서울 신사동 · 공식 '공간 안내'의 층 구성을 쌓아 올린 손그림")
    fl = [
        dict(tag="2-3F", title="Private Zone", tt=40, items=["Private Changing Room", "Warm Bath · Cold Shower", "Finnish Sauna · Ice Fountain"], blocks=[("steam", 30), ("wave", 22), ("plain", 20)]),
        dict(tag="1.5F", title="Touch Zone", items=["Pre-Sauna Touch Care"], blocks=[("desk", 20), ("plain", 18)]),
        dict(tag="1F", title="Welcoming Zone", items=["Check In", "Shop"], blocks=[("desk", 22), ("plain", 20)]),
    ]
    sv, S = tower(fl, 270, 186, top=236, bottom=772, t=24)
    b += sv
    st, ye = steps_box(60, 788, 480, "리추얼 순서(공식)", ["Warm-up · 터치 케어", "Totonoi · 프라이빗 존에서 사우나", "Cold Shower & Cold Bath", "Clarity · 마무리"], size=26)
    b += st
    sn, y2 = side_note(600, 788, 420, "이용 방식(공식)", ["100% 사전 예약제(네이버 예약)", "1인 이용을 원칙으로 해요", "예약자 본인 이용 원칙"], size=25)
    b += sn
    b += foot("sisu.house", True, ["공식 사이트의 '공간 안내'·'리추얼 단계'를 글자로 읽고 그렸어요."], y=1098)
    return b

def orakai():
    b = base("오라카이 청계산, 조감도", "서울 서초 · 호텔 지하 1층 사우나 안내를 쌓아 올린 손그림")
    fl = [
        dict(tag="B1", title="사우나(공식: 지하 1층)", items=["건식 사우나 · 습식 사우나", "다양한 탕", "세신은 현장 접수만 가능", "같은 층에 'LEVEL B1' 레스토랑이 있어요"], blocks=[("steam", 30), ("wave", 22), ("plain", 20)]),
        dict(tag="B2", title="주차 · 전기차 충전소", items=["전기차 충전소는 지하 2층(공식 이용안내)"], blocks=[("car", 20), ("plain", 16)]),
    ]
    sv, S = tower(fl, 270, 200, top=246, bottom=790, t=28)
    b += sv
    sn, y2 = side_note(60, 830, 470, "이용 안내(공식)", ["객실을 이용하지 않은 고객도 이용 가능", "8세 미만은 입장 불가", "8세 이상 17세 미만은 보호자 동반"], size=25)
    b += sn
    sn2, y3 = side_note(580, 830, 440, "알아 두세요", ["정기 휴관일이 있어요", "요금·운영시간은 상세 페이지에서 확인하세요", "사우나 안의 구체적인 배치는 공식 안내에 글자로 나와 있지 않아요"], size=25)
    b += sn2
    b += foot("cgs.orakaihotels.com", True, y=1098)
    return b

def heosimcheong():
    b = base("허심청, 조감도", "호텔농심 온천 · 공식 층별 시설 안내를 쌓아 올린 손그림")
    fl = [
        dict(tag="5F", title="온천시설 · 온천탕", items=["남·여 별도탕, 3,000명 동시 수용(공식)", "장수탕 · 이벤트탕 · 청자탕 · 노천탕 · 회목탕 · 물맞이탕 · 동굴탕 · 냉탕 · 반신욕탕 · 소금탕 · 미니풀장", "증기 사우나(습식) · 고온 사우나(건식) · 샤워장 · 세신코너"], blocks=[("wave", 22), ("steam", 28), ("wave", 20)]),
        dict(tag="4F", title="온천시설 · 입퇴장", items=["안내데스크(키 지급·계산)", "락카(남·여) · 이발소 · 구두수선실", "락카룸 용품판매코너에서 가운 받기"], blocks=[("desk", 22), ("locker", 26)]),
        dict(tag="3F", title="찜질방", items=["참숯방 · 아로마향기방 · 황토방 · 보석방 · 아이스방", "대휴게실 · 수면실 · 엔카페&스낵 · 헬스센터"], blocks=[("steam", 26), ("plain", 20)]),
        dict(tag="1-2F", title="식당 · 연회시설", items=["1F 제과점 · 중식당 · 패스트푸드점 · 카페 · 브로이하우스펍 · 다목적홀", "2F 연회장(대·중·소) · 예식홀(호텔 연결 통로)"], blocks=[("plain", 22), ("plain", 18)]),
        dict(tag="B1·2", title="주차장", items=["RV차량은 진입 금지(주차빌딩 이용)"], blocks=[("car", 16)]),
    ]
    sv, S = tower(fl, 262, 168, top=236, bottom=1062, size=23, wlab=520, lx=530, t=16)
    b += sv
    b += foot("hotelnongshim.com", True, ["이용 순서(공식): 4F 안내데스크에서 키 받고 입장 → 락카 → 5F 온천 · 3F 찜질 → 4F 계산(후불제)"], y=1092)
    return b

def supersapiens():
    b = base("슈퍼사피엔스, 조감도", "서울 강동 · 크로스핏 박스 공식 'Our Facility'를 쌓아 올린 손그림")
    fl = [
        dict(tag="3F", title="추가 시설", items=["회원용 태닝 시설", "스태프 전용 공간"], blocks=[("sun", 22), ("plain", 20)]),
        dict(tag="2F", title="편의 시설 · 보조 운동 공간", items=["샤워실 · 락커룸", "웨이트 · 스트레칭 · 유산소 존", "커뮤니티 공간"], blocks=[("locker", 26), ("plain", 20), ("plain", 18)]),
        dict(tag="1F", title="메인 운동 공간", items=["다양한 크로스핏 장비", "WOD · 오픈짐 이용 공간"], blocks=[("bar", 24), ("plain", 18)]),
    ]
    sv, S = tower(fl, 270, 196, top=236, bottom=820, t=28)
    b += sv
    sn, y2 = side_note(60, 850, 470, "주차(공식)", ["박스 앞 마당", "바로 옆 '함께하는교회' 건물 주차장"], size=26)
    b += sn
    sn2, y3 = side_note(580, 850, 440, "알아 두세요", ["공식 안내: 지상 3층 규모의 창고형 공간", "층 안의 구체적인 배치는 글자로 나와 있지 않아요", "드롭인·체험 예약은 상세 페이지에서 확인해요"], size=24)
    b += sn2
    b += foot("crossfitsupersapiens.com", False, y=1110)
    return b

def termeden():
    b = base("테르메덴, 조감도", "경기 이천 · 공식 시설 소개의 구역 이름을 한 장에 모은 손그림")
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
    b += text(540, fr[1] + 52, "입구 · 유인 매표소 · 2층 정산소(공식)", 27, "middle")
    cards = [
        (1, "실내 풀&스파", ["실내 바데풀(유수풀·유아풀)", "실내 이벤트탕 · 아로마 사우나(건식)", "플로팅 · 넥샤워 · 릴렉스 라이닝 등 수류 시설"]),
        (2, "온천대욕장", ["남·여 온천대욕장(온탕·냉탕·샤워)", "건식 · 습식 사우나 · 노천온천 · 이벤트탕", "노천탕과 대욕장은 홀딩도어로 이어져요(공식 소개)"]),
        (3, "찜질스파", ["편백나무방 · 소금방 · 황토방 · 맥반석방", "마사지체어룸 · 키즈라이브러리", "패밀리룸 · 가든커뮤니티 · 찜질복은 락커룸 데스크에서 대여"]),
        (4, "실외 풀&스파(1층·2층 구역)", ["인피니티풀 · 포레스트바스 · 워터플레이", "마운틴풀 · 테르메(정자탕·이벤트탕)", "카바나·슬라이드·파이어 연출은 시즌·시간대별 운영"]),
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
    b += foot("termeden.com", True, ["칸의 위치는 실제 배치가 아니에요 · 구역 이름만 글자로 확인했어요.", "휴장·클린데이·운영시간은 상세 페이지의 공지를 확인하세요."], y=1076)
    return b

SCENES = {
    "archive-sisu-house": (sisu, "시수하우스 조감도 손그림", "시수하우스 공식 공간 안내의 층 구성(1F Welcoming Zone, 1.5F Touch Zone, 2-3F Private Zone)과 리추얼 순서를 쌓아 올린 아이소메트릭 손그림. 구역 배치는 개념도예요."),
    "archive-spa-haesurang": (haesurang, "스파 해수랑 조감도 손그림", "스파 해수랑 공식 편의시설 안내의 층 표기(2F 로비, 3~4F 파우더룸, 4~5F 락커룸, 5F 찜질방, 7F 노천탕)를 층별로 쌓아 올린 아이소메트릭 손그림. 6층은 공식 안내가 없고 구역 배치는 개념도예요."),
    "archive-heosimcheong": (heosimcheong, "허심청 조감도 손그림", "호텔농심 허심청 공식 층별 시설 안내(B1·B2 주차장, 1F 식당, 2F 연회, 3F 찜질방, 4F 락카·안내데스크, 5F 온천탕)를 쌓아 올린 아이소메트릭 손그림. 구역 배치는 개념도예요."),
    "archive-termeden": (termeden, "테르메덴 조감도 손그림", "테르메덴 공식 시설 소개의 구역(실내 풀&스파, 온천대욕장, 찜질스파, 실외 풀&스파)을 한 장에 모은 아이소메트릭 손그림. 칸의 위치는 실제 배치가 아니에요."),
    "archive-orakai-cheonggyesan": (orakai, "오라카이 청계산 조감도 손그림", "오라카이 청계산 호텔 공식 안내의 지하 1층 사우나(건식·습식 사우나, 다양한 탕, 세신 현장 접수)와 지하 2층 전기차 충전소를 쌓아 올린 아이소메트릭 손그림. 사우나 안의 구체적인 배치는 그리지 않았어요."),
    "archive-crossfit-super-sapiens": (supersapiens, "크로스핏 슈퍼사피엔스 조감도 손그림", "크로스핏 슈퍼사피엔스 공식 시설 안내의 층 구성(1F 메인 운동 공간, 2F 샤워실·락커룸·보조 운동 공간, 3F 태닝·스태프 공간)을 쌓아 올린 아이소메트릭 손그림. 층 안의 배치는 개념도예요."),
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
