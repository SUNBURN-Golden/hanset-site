# -*- coding: utf-8 -*-
"""한셋 '조감도 손그림' (위에서 내려다본 평면 손그림), 2026-10-29.
공식 안내에 글자로 공개된 층·구역 이름만 사용. 사진·공식 지도·로고를 따라 그리거나 복사하지 않음.
칸의 크기·위치는 손그림 표현이며 실측이 아님. 가격·시간 미표기."""
import os, sys, math, html as _h
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from handdraw import *
import handdraw as h
from make_archive import wrap, fit, steam, waves

def cell(x, y, w, hh, label, kind, n=None):
    s = rect(x, y, w, hh, 3.2, fill=None)
    if kind == "pool":
        s += ellipse(x + w/2, y + hh/2, w*0.40, hh*0.34, 3.0, fill=0.06) + waves(x + w*0.25, y + hh/2 - 6, w*0.5, 1, 14)
    elif kind == "steam":
        s += hatch(x + 6, y + 6, w - 12, hh - 12, gap=16, lw=1.4, op=0.35) + steam(x + w/2 - 30, y + hh*0.62, 3)
    elif kind == "locker":
        k = max(3, int(w // 34))
        for i in range(k): s += rect(x + 12 + i*(w-24)/k, y + 12, (w-24)/k - 6, 22, 2.2, fill=0.12)
    elif kind == "room":
        s += hatch(x + 6, y + 6, w - 12, hh - 12, gap=22, ang=-1, lw=1.2, op=0.25)
    elif kind == "gym":
        s += line(x + w*0.3, y + hh*0.62, x + w*0.7, y + hh*0.62, 4.2) + rect(x + w*0.3 - 10, y + hh*0.62 - 16, 10, 32, 2.6, fill=0.5) + rect(x + w*0.7, y + hh*0.62 - 16, 10, 32, 2.6, fill=0.5)
    elif kind == "car":
        for i in range(3): s += rect(x + 14 + i*(w-28)/3, y + hh*0.35, (w-28)/3 - 10, hh*0.4, 2.2, dash="4 6")
    if n is not None: s += numdot(x + 28, y + 28, n, 20)
    sz = fit(label, 30, w - 20)
    for i, l in enumerate(wrap(label, sz, w - 20)[:2]):
        s += text(x + w/2, y + hh - 18 - (24 if len(wrap(label, sz, w-20)) > 1 and i == 0 else 0), l, sz, "middle")
    return s

def plate(x, y, w, hh, tag, cells):
    """한 층(또는 한 구역 묶음)을 위에서 본 판. cells: (label, kind, fx, fy, fw, fh)"""
    s = rect(x - 8, y - 8, w + 16, hh + 16, 2.2, dash="5 8", op=0.7)  # 외벽 그림자선
    s += rect(x, y, w, hh, 4.2, fill=0.03)
    s += text(x, y - 22, tag, fit(tag, 34, w), "start")
    for lab, kind, fx, fy, fw, fh in cells:
        s += cell(x + fx*w + 6, y + fy*hh + 6, fw*w - 12, fh*hh - 12, lab, kind)
    return s

def compass():
    s = ellipse(985, 245, 30, 30, 3) + line(985, 268, 985, 222, 3.4) + line(985, 222, 975, 238, 3) + line(985, 222, 995, 238, 3)
    return s + text(985, 207, "위", 24, "middle")

def sheet(title, sub, plates):
    b = header(title, sub) + compass()
    for p in plates: b += plate(*p)
    b += text(1020, 1312, "한셋 HANSET", 30, "end", rot=-1)
    return b

# 판 좌표: (x, y, w, h, tag, cells)
SCENES = {
 "aerial-termeden": ("테르메덴 조감도", "위에서 내려다본 네 구역", [
   (60, 300, 960, 900, "테르메덴 단지", [
     ("입구 · 매표소", "room", 0.36, 0.86, 0.28, 0.14),
     ("실내 풀&스파", "pool", 0.00, 0.50, 0.36, 0.36),
     ("온천대욕장", "pool", 0.36, 0.50, 0.32, 0.36),
     ("찜질스파", "steam", 0.68, 0.50, 0.32, 0.50),
     ("노천온천", "pool", 0.36, 0.30, 0.32, 0.20),
     ("인피니티풀", "pool", 0.00, 0.00, 0.36, 0.28),
     ("포레스트바스", "pool", 0.36, 0.00, 0.32, 0.30),
     ("마운틴풀 · 테르메", "pool", 0.68, 0.00, 0.32, 0.50),
     ("실외 워터플레이", "pool", 0.00, 0.28, 0.36, 0.22)])],
   "Bird's-eye hand drawing of Termeden in Icheon seen from above: the indoor pool and spa, the hot-spring bathhouse joined to its open-air bath, the jjimjil spa rooms, and the outdoor pools (infinity pool, forest bath, mountain pool, terme), with the entrance and ticket desk."),
 "aerial-spa-haesurang": ("스파 해수랑 조감도", "위에서 내려다본 층별 평면", [
   (60, 300, 450, 400, "7F 노천탕 · 사우나", [("노천탕", "pool", 0, 0, 0.6, 0.6), ("핀란드 사우나", "steam", 0.6, 0, 0.4, 0.6), ("라운지바", "room", 0, 0.6, 0.5, 0.4), ("PDR룸", "room", 0.5, 0.6, 0.5, 0.4)]),
   (570, 300, 450, 400, "5F 찜질방 · 락커룸", [("찜질방", "steam", 0, 0, 0.6, 1), ("락커룸", "locker", 0.6, 0, 0.4, 1)]),
   (60, 790, 450, 400, "4F 락커룸 · 파우더룸", [("락커룸", "locker", 0, 0, 0.55, 1), ("파우더룸", "room", 0.55, 0, 0.45, 1)]),
   (570, 790, 450, 400, "2F 로비 · 3F 파우더룸", [("로비", "room", 0, 0, 1, 0.5), ("3F 파우더룸", "room", 0, 0.5, 1, 0.5)])],
   "Bird's-eye hand drawing of Spa Haesurang in Busan, floor plans seen from above: the 7th-floor open-air bath, Finnish sauna, lounge bar and PDR rooms, the 5th-floor jjimjil room and lockers, the 4th-floor lockers and powder room, and the 2nd-floor lobby with the 3rd-floor powder room."),
 "aerial-heosimcheong": ("허심청 조감도", "위에서 내려다본 층별 평면", [
   (60, 300, 450, 400, "5F 온천탕", [("온천탕", "pool", 0, 0, 0.62, 1), ("사우나", "steam", 0.62, 0, 0.38, 1)]),
   (570, 300, 450, 400, "4F 락카 · 안내데스크", [("락카", "locker", 0, 0, 0.6, 1), ("안내데스크", "room", 0.6, 0, 0.4, 1)]),
   (60, 790, 450, 400, "3F 찜질방", [("찜질방", "steam", 0, 0, 0.6, 1), ("대휴게실 · 수면실", "room", 0.6, 0, 0.4, 1)]),
   (570, 790, 450, 400, "1F 식당 · 2F 연회 · B1·B2 주차", [("1F 식당", "room", 0, 0, 0.5, 0.5), ("2F 연회", "room", 0.5, 0, 0.5, 0.5), ("B1·B2 주차장", "car", 0, 0.5, 1, 0.5)])],
   "Bird's-eye hand drawing of Hurshimchung at Hotel Nongshim in Busan, floor plans seen from above: 5th-floor hot-spring baths, 4th-floor lockers and front desk, 3rd-floor jjimjil room, and the restaurant, banquet and parking floors below."),
 "aerial-orakai-cheonggyesan": ("오라카이 청계산 조감도", "위에서 내려다본 지하 사우나", [
   (60, 300, 960, 560, "B1 사우나", [("건식 사우나", "steam", 0, 0, 0.3, 0.5), ("습식 사우나", "steam", 0, 0.5, 0.3, 0.5), ("여러 탕", "pool", 0.3, 0, 0.45, 1), ("세신", "room", 0.75, 0, 0.25, 1)]),
   (60, 950, 960, 250, "B2 전기차 충전소", [("전기차 충전", "car", 0, 0, 1, 1)])],
   "Bird's-eye hand drawing of Orakai Cheonggyesan Hotel seen from above: the basement-1 sauna with dry and wet saunas, several pools and a body-scrub area, and the basement-2 EV charging area."),
 "aerial-sisu-house": ("시수하우스 조감도", "위에서 내려다본 층별 평면", [
   (60, 300, 450, 400, "1F Welcoming Zone", [("체크인", "room", 0, 0, 0.5, 1), ("숍", "room", 0.5, 0, 0.5, 1)]),
   (570, 300, 450, 400, "1.5F Touch Zone", [("터치 케어", "room", 0, 0, 1, 1)]),
   (60, 790, 960, 400, "2-3F Private Zone", [("핀란드 사우나", "steam", 0, 0, 0.4, 1), ("웜배스 · 콜드샤워", "pool", 0.4, 0, 0.3, 1), ("프라이빗 탈의실", "locker", 0.7, 0, 0.3, 1)])],
   "Bird's-eye hand drawing of Sisu House in Seoul, floor plans seen from above: the 1st-floor Welcoming Zone (check-in and shop), the 1.5-floor Touch Zone, and the 2nd–3rd-floor Private Zone with sauna, warm bath, cold shower and private changing room."),
 "aerial-crossfit-super-sapiens": ("슈퍼사피엔스 조감도", "위에서 내려다본 층별 평면", [
   (60, 300, 960, 440, "1F 메인 운동 공간", [("메인 트레이닝 플로어", "gym", 0, 0, 1, 1)]),
   (60, 830, 450, 370, "2F", [("샤워 · 락커", "locker", 0, 0, 0.5, 1), ("보조 운동", "gym", 0.5, 0, 0.5, 1)]),
   (570, 830, 450, 370, "3F", [("태닝", "room", 0, 0, 0.5, 1), ("스태프", "room", 0.5, 0, 0.5, 1)])],
   "Bird's-eye hand drawing of CrossFit Super Sapiens seen from above: the 1st-floor main training floor, the 2nd floor with showers, lockers and an accessory training area, and the 3rd floor with tanning and staff space."),
}

if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "svg")
    for k, (t, sub, plates, d) in SCENES.items():
        h.rnd.seed(29); h._clip[0] = 0
        open(f"{out}/{k}.svg", "w").write(svg(sheet(t, sub, plates), title=_h.escape(t), desc=_h.escape(d)))
        print(k)
