import sys; sys.path.insert(0, "/workspace/fitness-hub/illustrations")
from handdraw import *
import handdraw as h

NOTE = "일반 안내 그림이에요. 실제 업장과 다를 수 있어요."

# ---------------------------------------------------------------- 1. 크로스핏 박스 구조
def box_plan():
    b = header("크로스핏 박스, 이렇게 생겼어요", "일반적인 배치 예시 · 위에서 내려다본 그림")
    X, Y, W, H = 70, 250, 940, 880
    B = Y + H
    b += rect(X, Y, W, H, 6.5)
    # 입구(아래 왼쪽 벽 틈)
    b += f'<rect x="{X+30}" y="{B-9}" width="130" height="18" fill="{PAPER}"/>'
    b += text(X+95, B+44, "입구", 38, "middle")
    # 락커·탈의
    for i in range(6):
        b += rect(X+24+i*38, Y+22, 34, 60, 3, fill=0.06)
    b += text(X+24, Y+122, "락커·탈의", 32)
    b += text(X+24, Y+154, "(없는 곳도 있어요)", 25, op=0.85)
    # 화이트보드
    b += rect(440, Y+14, 320, 48, 4, fill=0.05)
    b += rpath([(460, Y+30), (530, Y+34), (600, Y+28)], 2.4, passes=1) + rpath([(460, Y+46), (550, Y+50), (660, Y+44)], 2.4, passes=1)
    b += text(600, Y+102, "화이트보드(오늘의 와드)", 30, "middle")
    # 와드 구역
    b += rect(105, 440, 490, 470, 3.2, dash="10 9")
    b += text(350, 540, "와드 구역", 56, "middle", rot=-1.5)
    b += text(350, 590, "수업을 하는 넓은 바닥", 31, "middle")
    b += text(350, 628, "워밍업·동작 연습·와드", 31, "middle")
    for (px, py) in [(165, 770), (225, 790)]:
        b += ellipse(px, py+8, 19, 17, 3.2) + rpath([(px-12, py-7), (px-8, py-24), (px+8, py-24), (px+12, py-7)], 3.2, passes=1)
    b += text(195, 850, "케틀벨", 27, "middle")
    b += rect(310, 750, 56, 56, 3.2) + rect(380, 760, 48, 48, 3.2) + rect(442, 770, 40, 40, 3.2)
    b += text(396, 850, "점프 박스", 27, "middle")
    b += stick(540, 805, 1.0, "up")
    # 리그
    rx, ry = 640, 450
    b += rect(rx, ry, 340, 36, 4, fill=0.08)
    for i in range(5):
        b += rect(rx+i*84-4, ry-10, 20, 56, 3, fill=0.3)
    for i in range(4):
        b += ellipse(rx+42+i*84, ry+18, 8, 8, 2.6)
    b += text(rx+170, ry-26, "리그 (풀업 바·링)", 35, "middle")
    b += text(rx+170, ry+86, "철봉·링에 매달리는 구조물", 27, "middle", op=0.9)
    # 오픈짐 / 랙
    for i in range(3):
        px = 660 + i*108
        b += rect(px, 640, 72, 120, 3.6, fill=0.04)
        b += line(px-14, 680, px+86, 680, 4.2) + rect(px-24, 668, 12, 24, 3, fill=0.35) + rect(px+86, 668, 12, 24, 3, fill=0.35)
    b += text(820, 826, "오픈짐 · 바벨 랙", 35, "middle")
    b += text(820, 864, "자유 운동 시간에 쓰는 곳", 27, "middle", op=0.9)
    # 로잉·바이크
    for i in range(4):
        px = 500 + i*66
        b += rect(px, 960, 40, 140, 3.2) + ellipse(px+20, 982, 16, 16, 3) + rect(px+8, 1050, 24, 36, 2.6, fill=0.15)
    b += text(790, 1010, "로잉·바이크", 35)
    b += text(790, 1048, "(유산소 기구)", 27, op=0.9)
    # 접수 데스크
    b += rect(215, 1022, 250, 78, 4, fill=0.06)
    b += text(340, 1073, "접수 데스크", 34, "middle")
    # 동선
    b += curve_arrow([(X+95, B-30), (X+85, B-150), (X+75, B-210), (X+80, B-230)], 3.8, "2 11")
    b += footer([NOTE, "드롭인 전에는 업장 공식 안내(시간표·요금·예약)를 꼭 확인하세요."])
    return b

# ---------------------------------------------------------------- 2. 드롭인 이용 순서
def doodle_calendar(x, y):
    s = rect(x, y, 120, 104, 4) + line(x, y+30, x+120, y+30, 4) + line(x+30, y-12, x+30, y+12, 4.5) + line(x+90, y-12, x+90, y+12, 4.5)
    for r in range(2):
        for c in range(4):
            s += ellipse(x+22+c*26, y+50+r*26, 4, 4, 2.4, fill=0.7)
    s += rpath([(x+84, y+70), (x+94, y+84), (x+112, y+58)], 4.2, passes=1)
    return s
def doodle_chat(x, y):
    s = rpath([(x, y), (x+130, y), (x+130, y+74), (x+54, y+74), (x+26, y+104), (x+30, y+74), (x, y+74)], 4, close=True)
    for i in range(3): s += ellipse(x+34+i*30, y+37, 5, 5, 2.4, fill=0.8)
    return s
def doodle_door(x, y):
    s = rect(x, y, 90, 130, 4) + rpath([(x+90, y+130), (x+150, y+110), (x+150, y-10), (x+90, y)], 3.6, passes=1, dash="3 8")
    s += ellipse(x+72, y+68, 5, 5, 2.4, fill=0.8)
    s += rect(x+6, y+112, 78, 14, 2.6, fill=0.12)
    return s
def doodle_barbell(x, y):
    s = line(x, y+40, x+170, y+40, 5)
    for dx in (-0, 14, 156, 170):
        pass
    s += rect(x-10, y+8, 14, 64, 3.4, fill=0.55) + rect(x+8, y+16, 12, 48, 3, fill=0.35)
    s += rect(x+166, y+8, 14, 64, 3.4, fill=0.55) + rect(x+150, y+16, 12, 48, 3, fill=0.35)
    s += stick(x+85, y+24, 0.0, "stand") if False else ""
    return s
def doodle_towel(x, y):
    s = rpath([(x+6, y+30), (x+6, y+120), (x+54, y+120), (x+54, y+30), (x+44, y+14), (x+16, y+14)], 4, close=True)
    s += rect(x+14, y-6, 28, 20, 3.4) + line(x+6, y+64, x+54, y+64, 3)
    s += rect(x+76, y+50, 90, 70, 3.8, fill=0.06) + line(x+76, y+72, x+166, y+72, 2.4) + line(x+76, y+94, x+166, y+94, 2.4)
    return s

def dropin_steps():
    b = header("드롭인, 이렇게 이용해요", "처음 가는 크로스핏 박스 · 이용 순서")
    items = [
        ("공식 페이지에서 확인", ["드롭인이 되는 수업 시간과", "요금을 업장 공식 페이지에서 봐요."], doodle_calendar),
        ("예약·문의", ["예약이 필요한지는 업장마다 달라요.", "공식 안내대로 신청하거나 문의해요."], doodle_chat),
        ("도착해서 접수", ["코치에게 '드롭인으로 왔어요'라고 말해요.", "신청서·안내가 있으면 읽고 작성해요."], doodle_door),
        ("수업 참여", ["보통 워밍업 → 연습 → 와드 → 마무리예요.", "힘들면 코치에게 말하고 강도를 낮춰요."], doodle_barbell),
        ("정리와 결제", ["결제 방법과 다음 이용은 업장 안내를 따라요.", "물·수건·운동화는 미리 챙겨 가요."], doodle_towel),
    ]
    y = 255
    for i, (t, ds, fn) in enumerate(items):
        top = y + i * 186
        b += numdot(88, top + 78, i + 1, 30)
        b += rect(150, top, 850, 158, 3.4, fill=0.03)
        b += fn(180, top + 30)
        b += text(385, top + 60, t, 48)
        b += lines_text(385, top + 106, ds, 33, 1.3)
        if i < 4: b += curve_arrow([(88, top + 120), (92, top + 150), (88, top + 176)], 3.4, "2 9", head=13)
    b += footer([NOTE, "수업 방식·요금·예약 방법은 업장마다 달라요. 공식 안내를 먼저 확인하세요."], y0=1245)
    return b

# ---------------------------------------------------------------- 3. 사우나 이용 순서 / 건식·습식
def sauna_plan():
    b = header("사우나, 이렇게 이용해요", "이용 순서와 건식·습식 일반 구조 예시")
    X, Y, W, H = 70, 240, 940, 470
    b += rect(X, Y, W, H, 6.5)
    # 칸막이 벽
    b += line(330, Y, 330, Y+H, 5) + line(330, Y+250, X+W, Y+250, 5) + line(640, Y, 640, Y+250, 5) + line(330, Y+250, 330, Y+H, 5)
    # 문 틈
    b += f'<rect x="{X-8}" y="{Y+320}" width="16" height="90" fill="{PAPER}"/>'
    b += text(X-4, Y+H+36, "입구", 30, "start")
    # 입구·락커 (왼쪽)
    for i in range(5):
        b += rect(X+18, Y+24+i*52, 78, 44, 3, fill=0.08)
    b += text(X+120, Y+80, "락커", 34) + text(X+120, Y+118, "탈의", 34)
    b += numdot(X+190, Y+190, 1, 22)
    b += text(X+14, Y+310, "이용 대상·", 26, op=0.0)
    # 샤워 구역 (아래 왼쪽→가운데)
    b += hatch(95, Y+330, 215, 120, 22, 1, 1.8, 0.35)
    for i in range(3):
        px = 130 + i*70
        b += ellipse(px, Y+372, 17, 11, 3) + rpath([(px-8, Y+385), (px-14, Y+412)], 2.6, passes=1) + rpath([(px, Y+388), (px, Y+416)], 2.6, passes=1) + rpath([(px+8, Y+385), (px+14, Y+412)], 2.6, passes=1)
    b += text(150, Y+323, "샤워", 34, "middle")
    b += numdot(285, Y+300, 2, 22)
    # 건식 사우나 (위 가운데)
    b += hatch(335, Y+8, 300, 234, 26, -1, 1.6, 0.18)
    b += rect(360, Y+60, 250, 40, 3.8, fill=0.06) + rect(360, Y+110, 250, 40, 3.8, fill=0.06) + rect(360, Y+160, 250, 40, 3.8, fill=0.06)
    b += rect(555, Y+214, 45, 24, 3.2, fill=0.55)  # 히터
    b += text(485, Y+40, "건식 사우나", 38, "middle")
    b += text(487, Y+232, "건조한 열 · 나무 벤치", 25, "middle", op=0.0)
    b += numdot(372, Y+232, 3, 20)
    # 습식 (위 오른쪽)
    b += text(830, Y+40, "습식 사우나", 38, "middle")
    for i, px in enumerate([720, 790, 860, 930]):
        b += rpath([(px, Y+205), (px-18, Y+170), (px+14, Y+140), (px-14, Y+108), (px+8, Y+78)], 3.6, passes=1)
    b += numdot(675, Y+232, 3, 20)
    b += text(830, Y+236, "수증기 · 습도가 높음", 26, "middle", op=0.85)
    b += text(485, Y+267-30, "", 1)
    # 냉수욕 (아래 가운데)
    b += rect(355, Y+290, 260, 140, 4.2, fill=0.03)
    for i in range(2):
        b += rpath([(375, Y+330+i*32), (420, Y+318+i*32), (470, Y+336+i*32), (520, Y+320+i*32), (595, Y+334+i*32)], 3, passes=1, op=0.7)
    b += text(485, Y+283, "", 1)
    b += text(485, Y+418, "냉수욕 (선택)", 34, "middle")
    b += numdot(640, Y+310, 4, 20)
    # 휴게 (아래 오른쪽)
    b += rect(680, Y+290, 80, 40, 3.6, fill=0.08) + rect(790, Y+290, 80, 40, 3.6, fill=0.08) + rect(900, Y+290, 80, 40, 3.6, fill=0.08)
    b += rpath([(680, Y+330), (680, Y+345)], 3, passes=1) + rpath([(870, Y+330), (870, Y+345)], 3, passes=1)
    b += ellipse(720, Y+385, 16, 22, 3) + rect(712, Y+358, 16, 8, 2.6)
    b += text(830, Y+440, "휴게 공간 · 물 마시기", 34, "middle")
    b += numdot(702, Y+265, 5, 20) if False else ""
    # 동선 화살표
    b += curve_arrow([(X+130, Y+215), (190, Y+290), (225, Y+322)], 3.4, "2 10", head=14)
    b += text(455, Y+h.rnd.randint(0, 0) + 0, "", 1)
    # 순서 목록(아래)
    y0 = 770
    b += text(60, y0, "이용 순서", 42)
    b += rpath([(60, y0+14), (220, y0+18), (260, y0+12)], 3.4, passes=1)
    steps = [
        "이용 대상·요금·휴무일을 공식 안내에서 먼저 확인해요.",
        "입장해 샤워로 몸을 씻고 땀·물기를 닦아요.",
        "사우나에 들어가요. 처음에는 짧게, 무리하지 않아요.",
        "나와서 몸을 식혀요. 냉수욕은 하지 않아도 괜찮아요.",
        "휴게 공간에서 쉬고 물을 마셔요.",
        "컨디션을 보며 한 번 더 하거나 마무리해요.",
    ]
    for i, t in enumerate(steps):
        yy = y0 + 56 + i * 52
        b += numdot(82, yy - 11, i + 1, 19) if False else text(60, yy, f"{i+1}.", 32)
        b += text(105, yy, t, 31)
    # 안전 박스
    sy = 1146
    b += rect(60, sy, 960, 112, 4, fill=0.06, dash="14 7")
    b += text(86, sy+42, "꼭 확인하세요", 32)
    b += text(86, sy+78, "심혈관 질환·임신·음주 후·컨디션 불량 시 사우나·냉수욕을 피하거나 의사와 상담하세요.", 26.5)
    b += text(86, sy+104, "의학적 조언이 아닙니다.", 26.5)
    b += text(60, 1298, NOTE + " 업장마다 시설과 이용 방법이 달라요.", 27, op=0.9)
    b += text(1020, 1334, "한셋 HANSET", 30, "end", rot=-1)
    return b

SCENES = {
    "crossfit-box-plan": (box_plan, "크로스핏 박스의 일반적인 구조 그림", "위에서 내려다본 평면도 스타일의 손그림. 입구와 접수 데스크, 락커·탈의 공간, 화이트보드, 와드 구역, 리그, 오픈짐·바벨 랙, 로잉·바이크 구역의 일반적인 배치를 보여 줍니다. 실제 업장과 다를 수 있습니다."),
    "dropin-steps": (dropin_steps, "크로스핏 드롭인 이용 순서 그림", "공식 페이지에서 시간표와 요금 확인, 예약·문의, 도착해서 접수, 수업 참여, 정리와 결제의 다섯 단계를 손그림으로 보여 줍니다. 수업 방식과 요금은 업장마다 다릅니다."),
    "sauna-guide": (sauna_plan, "사우나 이용 순서와 건식·습식 구조 그림", "락커, 샤워, 건식 사우나, 습식 사우나, 냉수욕, 휴게 공간의 일반적인 배치와 이용 순서를 손그림으로 보여 줍니다. 심혈관 질환·임신·음주 후·컨디션 불량 시에는 사우나를 피하거나 의사와 상담해야 합니다. 업장마다 시설이 다릅니다."),
}
if __name__ == "__main__":
    import os
    os.makedirs("/workspace/fitness-hub/illustrations/svg", exist_ok=True)
    for k, (fn, t, d) in SCENES.items():
        h.rnd.seed(11)
        open(f"/workspace/fitness-hub/illustrations/svg/{k}.svg", "w").write(svg(fn(), title=t, desc=d))
        print(k)
