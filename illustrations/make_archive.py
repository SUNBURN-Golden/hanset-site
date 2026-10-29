# -*- coding: utf-8 -*-
"""한셋 '손그림 아카이브' 손그림 (2026-10-18).
공식 홈페이지·공식 안내에 글자로 공개된 층·구역 이름만 사용해 새로 그림(원본 이미지 복사·트레이싱 없음).
공식 출처에 없는 구조·위치·치수는 그리지 않는다."""
import sys; sys.path.insert(0, "/workspace/fitness-hub/illustrations")
from handdraw import *
import handdraw as h

CHECK = "2026.10.18"
LABEL = "공식 안내를 바탕으로 한 손그림 · 실제와 다를 수 있어요"
SAFE = ["심혈관 질환·임신·음주 후·컨디션 불량 시 사우나·냉수욕을 피하거나", "의사와 상담하세요. 의학적 조언이 아닙니다."]

def wrap(s, size, maxw):
    toks = s.split(" "); lines = []; cur = ""
    for t in toks:
        cand = (cur + " " + t) if cur else t
        if tw(cand, size) <= maxw: cur = cand; continue
        if cur: lines.append(cur); cur = ""
        while tw(t, size) > maxw:  # 긴 토큰은 글자 단위로 끊기
            k = len(t)
            while k > 1 and tw(t[:k], size) > maxw: k -= 1
            lines.append(t[:k]); t = t[k:]
        cur = t
    if cur: lines.append(cur)
    return lines

def fit(s, size, maxw):
    while tw(s, size) > maxw and size > 20: size -= 1
    return size

def head(title, sub):
    sz = fit(title, 76, 960)
    s = text(60, 118, title, sz, "start", rot=-0.8)
    s += rpath([(60, 142), (300, 148), (min(60 + tw(title, sz), 900), 141)], 4.5, passes=2)
    s += text(62, 196, sub, fit(sub, 32, 960), "start", op=0.85)
    return s

def foot(src, sauna=True, extra=None):
    y = 1098
    s = rpath([(60, y), (540, y + 4), (1020, y - 1)], 2.4, passes=1, dash="3 9", op=0.8)
    s += text(60, y + 44, LABEL, fit(LABEL, 30, 960))
    s += text(60, y + 80, f"출처 {src} · 확인일 {CHECK}", fit(f"출처 {src} · 확인일 {CHECK}", 27, 960), op=0.9)
    yy = y + 114
    if extra:
        for e in extra:
            s += text(60, yy, e, fit(e, 25, 960), op=0.85); yy += 31
    if sauna:
        for e in SAFE:
            s += text(60, yy, e, fit(e, 24, 960), op=0.85); yy += 29
    s += text(1020, 1334, "한셋 HANSET", 30, "end", rot=-1)
    return s

def para(x, y, s, size, maxw, lh=1.28, op=1.0, bullet=False):
    out = ""; n = 0
    lines = wrap(("· " + s) if bullet else s, size, maxw)
    for i, l in enumerate(lines):
        if bullet and i > 0: out += text(x + size * 0.9, y + i * size * lh, l, size, op=op)
        else: out += text(x, y + i * size * lh, l, size, op=op)
    return out, len(lines)

def waves(x, y, w, n=2, gap=22):
    s = ""
    for i in range(n):
        pts = [(x + k * w / 6, y + i * gap + (6 if k % 2 else -6)) for k in range(7)]
        s += rpath(pts, 3, passes=1, op=0.75)
    return s

def lockers(x, y, n=5):
    return "".join(rect(x + i * 34, y, 30, 52, 3, fill=0.08) for i in range(n))

def steam(x, y, n=3):
    s = ""
    for i in range(n):
        px = x + i * 30
        s += rpath([(px, y + 44), (px - 10, y + 26), (px + 8, y + 12), (px - 8, y - 4)], 3.2, passes=1)
    return s

def floors(rows, x, y0, w, gap=14, tagw=100, size=28, lh=1.26, inner_pad=18, fill_to=None):
    """rows: dict(tag, title, items, dashed, note, deco) — 위에서 아래로 그림. 반환 (svg, 끝 y, [row boxes])."""
    s = ""; y = y0; boxes = []
    cw = w - tagw - inner_pad
    pre = []
    for r in rows:
        lines = [wrap("· " + it, size, cw) for it in r.get("items", [])]
        nlines = sum(len(l) for l in lines)
        hh = max(r.get("minh", 80), 22 + 36 + nlines * size * lh + 16) if r.get("items") else max(r.get("minh", 80), 76)
        pre.append((lines, hh))
    if fill_to:
        tot = sum(hh for _, hh in pre) + gap * (len(rows) - 1)
        extra = max(0, (fill_to - y0 - tot) / len(rows)); extra = min(extra, 90)
    else: extra = 0
    for r, (lines, hh) in zip(rows, pre):
        hh += extra
        tsz = fit(r["title"], 38, cw)
        s += rect(x, y, w, hh, 4.2, fill=(0.0 if r.get("dashed") else 0.04), dash=("12 9" if r.get("dashed") else None))
        cx, cy = x + tagw / 2, y + 44
        s += ellipse(cx, cy, 34, 34, 3.6)
        tg = r["tag"]; ts = fit(tg, 34, 58)
        s += text(cx, cy + 12, tg, ts, "middle", jitter=False)
        s += text(x + tagw, y + 46, r["title"], tsz)
        yy = y + 46 + size * 1.25
        for l in lines:
            for i, t in enumerate(l):
                s += text(x + tagw + (size * 0.9 if i > 0 else 0), yy, t, size); yy += size * lh
        if r.get("deco"): s += r["deco"](x, y, w, hh)
        boxes.append((x, y, w, hh)); y += hh + gap
    return s, y, boxes

def steps_box(x, y, w, title, steps, size=27, num_r=19):
    s = text(x, y + 36, title, 38) + rpath([(x, y + 50), (x + 100, y + 54), (x + 140, y + 49)], 3.4, passes=1)
    yy = y + 98
    for i, t in enumerate(steps):
        lines = wrap(t, size, w - 52)
        s += numdot(x + num_r, yy - 8, i + 1, num_r) if False else ellipse(x + num_r, yy - 9, num_r, num_r, 3.2) + text(x + num_r, yy + 3, str(i + 1), 28, "middle", jitter=False)
        for j, l in enumerate(lines): s += text(x + 52, yy + j * size * 1.25, l, size)
        yy += len(lines) * size * 1.25 + 26
    return s, yy

def side_note(x, y, w, title, items, size=26):
    s = rect(x, y, w, 0.1, 0.1)  # placeholder (no-op)
    s = text(x, y + 34, title, 34)
    yy = y + 76
    for it in items:
        ls = wrap("· " + it, size, w)
        for i, l in enumerate(ls):
            s += text(x + (size * 0.9 if i > 0 else 0), yy, l, size); yy += size * 1.26
        yy += 8
    return s, yy

def up_arrow(x, ytop, ybot):
    return curve_arrow([(x, ybot), (x + 4, (ytop + ybot) / 2), (x - 2, ytop)], 3.6, "2 10", head=15)

# ---------------------------------------------------------------- 1. 허심청
def heosimcheong():
    b = head("허심청, 층별로 보면", "호텔농심 온천 · 공식 층별 시설 안내를 손그림으로")
    rows = [
        dict(tag="5F", title="온천시설 · 온천탕", items=["남·여 별도탕, 3,000명 동시 수용(공식)", "장수탕 · 이벤트탕 · 청자탕 · 노천탕 · 회목탕 · 물맞이탕 · 동굴탕 · 냉탕 · 반신욕탕 · 소금탕 · 미니풀장", "증기 사우나(습식) · 고온 사우나(건식) · 샤워장 · 세신코너"], deco=lambda x, y, w, hh: waves(x + w - 120, y + 16, 90, 2, 16)),
        dict(tag="4F", title="온천시설 · 입퇴장", items=["안내데스크(키 지급·계산)", "락카(남·여) · 이발소 · 구두수선실", "락카룸 용품판매코너에서 가운 받기"], deco=lambda x, y, w, hh: lockers(x + w - 190, y + 12, 5)),
        dict(tag="3F", title="찜질방", items=["참숯방 · 아로마향기방 · 황토방 · 보석방 · 아이스방", "대휴게실 · 수면실 · 엔카페&스낵 · 헬스센터"], deco=lambda x, y, w, hh: steam(x + w - 100, y + 12, 3)),
        dict(tag="1-2F", title="식당 · 연회시설", items=["1F 제과점 · 중식당 · 패스트푸드점 · 카페 · 브로이하우스펍 · 다목적홀", "2F 연회장(대·중·소) · 예식홀(호텔 연결 통로)"]),
        dict(tag="B1·2", title="주차장", items=["RV차량은 진입 금지(주차빌딩 이용)"], minh=70),
    ]
    sv, yend, bx = floors(rows, 60, 226, 640, gap=9, tagw=108, size=25, lh=1.2, fill_to=1070)
    b += sv
    X = 740
    st, ye = steps_box(X, 236, 290, "이용 순서(후불제)", ["4F 안내데스크에서 키를 받고 입장", "락카에서 옷을 갈아입기(찜질은 가운 착용)", "5F 온천탕 · 3F 찜질·휴게실 이용", "4F 안내데스크에서 계산하고 퇴장"], size=27)
    b += st
    sn, y2 = side_note(X, ye + 6, 290, "이용 시간(공식)", ["온천 이용 시 4시간", "온천+찜질 6시간", "부대업장 포함 최대 7시간", "초과 시 1시간 단위 추가요금"], size=26)
    b += sn
    b += text(X, y2 + 8, "요금·운영시간은 상세 페이지에서", 24, op=0.85) + text(X, y2 + 36, "확인일과 함께 확인하세요.", 24, op=0.85)
    b += foot("hotelnongshim.com", True, ["층 구성만 글자로 확인한 그림이에요. 층 안의 위치·크기는 그리지 않았어요."])
    return b

# ---------------------------------------------------------------- 2. 스파 해수랑
def haesurang():
    b = head("스파 해수랑, 층 안내", "부산 서구 · 공식 편의시설 층 표기를 손그림으로")
    rows = [
        dict(tag="7F", title="노천탕", items=["노천탕 · 핀란드 사우나", "라운지바 · 대형스크린", "PDR룸(블루 웨이브 · 레드 하모니 · 퍼플 스카이)"], deco=lambda x, y, w, hh: waves(x + w - 120, y + 14, 90, 2, 16)),
        dict(tag="6F", title="공식 층 안내 없음", items=[], dashed=True, minh=70),
        dict(tag="5F", title="찜질방", items=["릴렉스존 · 안마의자존 · 동굴존 · 포토존 · 인형뽑기 · 키즈존", "스낵바 · 푸드코트", "사우나 락커룸은 4~5F(공식 층 표기)"], deco=lambda x, y, w, hh: steam(x + w - 100, y + 10, 3)),
        dict(tag="4F", title="락커룸 · 파우더룸", items=["사우나 락커룸 4~5F · 파우더룸 3~4F(공식 층 표기)"]),
        dict(tag="3F", title="파우더룸", items=["면봉 · 빗 · 드라이기가 있는 공간(공식)"]),
        dict(tag="2F", title="로비", items=["키오스크 · 신발장 · 인포데스크"]),
    ]
    sv, yend, bx = floors(rows, 60, 228, 640, gap=9, tagw=108, size=26, lh=1.2, fill_to=1060)
    b += sv
    X = 740
    b += text(X, 266, "사우나 구성(공식)", 36) + rpath([(X, 280), (X + 100, 284), (X + 150, 279)], 3.4, passes=1)
    yy = 328
    for it in ["습식 사우나", "건식 사우나", "온탕 · 열탕", "냉탕", "이벤트탕", "샤워 공간(80여 개)", "파우더룸"]:
        b += text(X, yy, "· " + it, 28); yy += 38
    b += text(X, yy + 10, "층 표기는 공식 사이트에", 25, op=0.85) + text(X, yy + 40, "없어서 칸에 넣지 않았어요.", 25, op=0.85)
    yy += 110
    s2, y2 = steps_box(X, yy, 290, "이용 순서(일반)", ["2F 로비에서 접수", "락커룸에서 갈아입기", "사우나·찜질·노천탕 이용"], size=27)
    b += s2
    b += foot("spahaesurang.co.kr", True, ["층 이름만 글자로 확인한 그림이에요. 층 안의 위치·크기는 그리지 않았어요."])
    return b

# ---------------------------------------------------------------- 3. 테르메덴
def card(x, y, w, hhh, tag, title, items, size=25, deco=None):
    s = rect(x, y, w, hhh, 4.2, fill=0.04)
    s += text(x + 18, y + 44, title, fit(title, 36, w - 36))
    yy = y + 44 + size * 1.3
    for it in items:
        for i, l in enumerate(wrap("· " + it, size, w - 36)):
            s += text(x + 18 + (size * 0.9 if i > 0 else 0), yy, l, size); yy += size * 1.22
    if deco: s += deco(x, y, w, hhh)
    return s

def termeden():
    b = head("테르메덴, 구역 안내", "경기 이천 · 공식 시설 소개의 구역 이름을 손그림으로")
    X1, X2, W, CH = 60, 550, 470, 350
    y1 = 232
    b += card(X1, y1, W, CH, "", "실내 풀&스파", ["실내 바데풀(유수풀·유아풀)", "실내 이벤트탕", "아로마 사우나(건식)", "플로팅 · 넥샤워 · 릴렉스 라이닝 등 수류 시설"], size=27, deco=lambda x, y, w, hh: waves(x + w - 120, y + 16, 90, 2, 18))
    b += card(X2, y1, W, CH, "", "온천대욕장", ["남·여 온천대욕장(온탕·냉탕·샤워)", "건식 · 습식 사우나", "노천온천 · 이벤트탕", "노천탕과 대욕장은 홀딩도어로 이어져요(공식 소개)"], size=27, deco=lambda x, y, w, hh: steam(x + w - 110, y + 14, 3))
    y2 = y1 + CH + 16
    b += card(X1, y2, W, CH, "", "찜질스파", ["편백나무방 · 소금방", "황토방 · 맥반석방", "마사지체어룸 · 키즈라이브러리", "패밀리룸 · 가든커뮤니티", "찜질복은 락커룸 데스크에서 대여"], size=27)
    b += card(X2, y2, W, CH, "", "실외 풀&스파(1층·2층 구역)", ["인피니티풀 · 포레스트바스", "워터플레이 · 마운틴풀", "테르메(정자탕·이벤트탕)", "카바나·슬라이드·파이어 연출은 시즌·시간대별 운영"], size=27)
    y3 = y2 + CH + 26
    b += rect(60, y3, 960, 90, 4, fill=0.06)
    b += text(540, y3 + 38, "입구 · 유인 매표소 · 2층 정산소", 38, "middle")
    b += text(540, y3 + 74, "사전 구매 티켓도 당일 현장 발권기에서 입장권으로 바꿔요(공식 이용가이드)", 26, "middle", op=0.9)
    b += up_arrow(300, y2 + CH + 4, y3 - 2) + up_arrow(790, y2 + CH + 4, y3 - 2)
    b += foot("termeden.com", True, ["칸의 위치는 실제 배치가 아니에요. 구역 이름만 글자로 확인했어요.", "휴장·클린데이·운영시간은 상세 페이지의 공지를 확인하세요."])
    return b

# ---------------------------------------------------------------- 4. 시수하우스
def sisu():
    b = head("시수하우스, 공간 안내", "서울 신사동 · 공식 '공간 안내' 층 구성을 손그림으로")
    rows = [
        dict(tag="2-3F", title="Private Zone", items=["Private Changing Room", "Warm Bath · Cold Shower", "Finnish Sauna · Ice Fountain"], deco=lambda x, y, w, hh: steam(x + w - 110, y + 14, 3), minh=170),
        dict(tag="1.5F", title="Touch Zone", items=["Pre-Sauna Touch Care"], minh=112),
        dict(tag="1F", title="Welcoming Zone", items=["Check In", "Shop"], minh=130),
    ]
    sv, yend, bx = floors(rows, 60, 240, 640, gap=26, tagw=116, size=31, lh=1.28, fill_to=1060)
    b += sv
    # 층 사이 올라가는 화살표
    for k in range(2):
        xa = 640; ytop = bx[k][1] + bx[k][3]; ybot = bx[k + 1][1]
        b += up_arrow(xa, ytop - 4, ybot + 4)
    X = 740
    st, ye = steps_box(X, 244, 290, "리추얼 순서(공식)", ["Warm-up · 터치 케어", "Totonoi · 프라이빗 존에서 사우나", "Cold Shower & Cold Bath", "Clarity · 마무리"], size=26)
    b += st
    sn, y2 = side_note(X, ye + 6, 290, "이용 방식", ["100% 사전 예약제(네이버 예약)", "1인 이용을 원칙으로 해요", "예약자 본인 이용 원칙"], size=25)
    b += sn
    b += foot("sisu.house", True, ["공식 사이트의 '공간 안내'·'리추얼 단계'를 글자로 읽고 그렸어요."])
    return b

# ---------------------------------------------------------------- 5. 오라카이 청계산
def orakai():
    b = head("오라카이 청계산 사우나", "서울 서초 · 호텔 지하 1층 사우나 안내를 손그림으로")
    rows = [
        dict(tag="B1", title="사우나 (공식: 지하 1층)", items=["건식 사우나 · 습식 사우나", "다양한 탕", "세신은 현장 접수만 가능(매주 월요일 휴무)", "같은 층에 'LEVEL B1' 레스토랑이 있어요"], deco=lambda x, y, w, hh: steam(x + w - 110, y + 14, 3), minh=250),
        dict(tag="B2", title="주차 · 전기차 충전소", items=["전기차 충전소는 지하 2층(공식 이용안내)", "사우나 이용 시 주차 3시간 무료(공식 이용안내)"], minh=170),
    ]
    sv, yend, bx = floors(rows, 60, 250, 640, gap=26, tagw=116, size=30, lh=1.26, fill_to=900)
    b += sv
    b += up_arrow(640, bx[0][1] + bx[0][3] - 2, bx[1][1] + 2)
    X = 740
    sn, y2 = side_note(X, 250, 290, "이용 안내(공식)", ["객실을 이용하지 않은 고객도 이용 가능", "8세 미만은 입장 불가", "8세 이상 17세 미만은 보호자 동반", "정기 휴관일이 있어요", "마지막 입장은 폐장 30분 전"], size=26)
    b += sn
    b += text(X, y2 + 14, "요금·운영시간·휴관일은", 24, op=0.85) + text(X, y2 + 42, "상세 페이지에서 확인하세요.", 24, op=0.85)
    b += text(60, yend + 36, "공식 안내에는 사우나 안의 구체적인 배치가 글자로 나와 있지 않아요.", 28, op=0.9)
    b += text(60, yend + 72, "그래서 층 구성과 시설 이름만 그렸어요.", 28, op=0.9)
    b += foot("cgs.orakaihotels.com", True)
    return b

# ---------------------------------------------------------------- 6. 크로스핏 슈퍼사피엔스
def supersapiens():
    b = head("슈퍼사피엔스, 층별 안내", "서울 강동 · 크로스핏 박스 공식 'Our Facility'를 손그림으로")
    def barbell(x, y, w, hh):
        px = x + w - 210; py = y + 22
        return line(px, py + 30, px + 170, py + 30, 5) + rect(px - 10, py + 6, 14, 48, 3.4, fill=0.55) + rect(px + 166, py + 6, 14, 48, 3.4, fill=0.55)
    rows = [
        dict(tag="3F", title="추가 시설", items=["회원용 태닝 시설", "스태프 전용 공간"], minh=130),
        dict(tag="2F", title="편의 시설 · 보조 운동 공간", items=["샤워실 · 락커룸", "웨이트 · 스트레칭 · 유산소 존", "커뮤니티 공간"], minh=170, deco=lockers_deco),
        dict(tag="1F", title="메인 운동 공간", items=["다양한 크로스핏 장비", "WOD · 오픈짐 이용 공간"], minh=150, deco=barbell),
    ]
    sv, yend, bx = floors(rows, 60, 240, 640, gap=26, tagw=116, size=31, lh=1.28, fill_to=930)
    b += sv
    for k in range(2):
        b += up_arrow(640, bx[k][1] + bx[k][3] - 2, bx[k + 1][1] + 2) if False else ""
    X = 740
    sn, y2 = side_note(X, 244, 290, "주차(공식)", ["박스 앞 마당", "바로 옆 '함께하는교회' 건물 주차장"], size=26)
    b += sn
    sn2, y3 = side_note(X, y2 + 14, 290, "알아 두세요", ["드롭인·체험 예약과 요금은 상세 페이지에서 확인해요", "수업 방식은 박스마다 달라요"], size=25)
    b += sn2
    b += text(60, yend + 36, "공식 안내는 '지상 3층 규모의 창고형 공간'이라고 해요.", 28, op=0.9)
    b += text(60, yend + 72, "층 안의 구체적인 배치는 글자로 나와 있지 않아 그리지 않았어요.", 28, op=0.9)
    b += foot("crossfitsupersapiens.com", False)
    return b

def lockers_deco(x, y, w, hh): return lockers(x + w - 190, y + hh - 70, 5)

SCENES = {
    "archive-orakai-cheonggyesan": (orakai, "오라카이 청계산 호텔 사우나 안내 손그림", "공식 안내에 나온 지하 1층 사우나(건식·습식 사우나, 다양한 탕, 세신 현장 접수)와 지하 2층 전기차 충전소, 이용 안내를 정리한 손그림. 사우나 안의 구체적인 배치는 그리지 않았어요."),
    "archive-heosimcheong": (heosimcheong, "허심청 층별 시설 안내 손그림", "호텔농심 허심청의 공식 층별 시설 안내(B1·B2 주차장, 1F 식당, 2F 연회, 3F 찜질방, 4F 락카·안내데스크, 5F 온천탕)와 이용 순서를 정리한 손그림."),
    "archive-termeden": (termeden, "테르메덴 구역 안내 손그림", "테르메덴 공식 시설 소개의 구역(실내 풀&스파, 온천대욕장, 찜질스파, 실외 풀&스파)과 구성 시설 이름을 정리한 손그림. 칸의 위치는 실제 배치가 아니에요."),
    "archive-spa-haesurang": (haesurang, "스파 해수랑 층 안내 손그림", "스파 해수랑 공식 편의시설 안내의 층 표기(2F 로비, 3~4F 파우더룸, 4~5F 락커룸, 5F 찜질방, 7F 노천탕)와 사우나 구성을 정리한 손그림. 6층은 공식 안내가 없어요."),
    "archive-sisu-house": (sisu, "시수하우스 공간 안내 손그림", "시수하우스 공식 공간 안내의 층 구성(1F Welcoming Zone, 1.5F Touch Zone, 2-3F Private Zone)과 리추얼 순서를 정리한 손그림."),
    "archive-crossfit-super-sapiens": (supersapiens, "크로스핏 슈퍼사피엔스 층별 안내 손그림", "크로스핏 슈퍼사피엔스 공식 시설 안내의 층 구성(1F 메인 운동 공간, 2F 샤워실·락커룸·보조 운동 공간, 3F 태닝·스태프 공간)과 주차 안내를 정리한 손그림."),
}
if __name__ == "__main__":
    import os
    os.makedirs("/workspace/fitness-hub/illustrations/svg", exist_ok=True)
    for k, (fn, t, d) in SCENES.items():
        h.rnd.seed(11)
        import html as _h
        open(f"/workspace/fitness-hub/illustrations/svg/{k}.svg", "w").write(svg(fn(), title=_h.escape(t), desc=_h.escape(d)))
        print(k)
