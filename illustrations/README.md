# 한셋 손그림 일러스트 (일반 안내 그림)
- 원본: `svg/*.svg` (1080×1350, 글자는 윤곽선 변환이라 폰트 없이 동일하게 보임). 같은 원본을 사이트(`site/static/img/guide/`)·인스타(PNG)·유튜브에 사용.
- PNG(1080×1350, 인스타 규격): `png/*.png`
- 생성: `python3 make_all.py && ./render_png.sh` (helper `handdraw.py`). 글꼴 Gaegu Bold (SIL OFL, `_fonts/OFL.txt`).
- 3종: crossfit-box-plan(박스 일반 구조), dropin-steps(드롭인 이용 순서), sauna-guide(사우나 순서·건식/습식 일반 구조)
- 규칙: 실제 업장의 구조를 그리지 않음(일반 안내 그림). 모든 그림에 "실제 업장과 다를 수 있어요" 표기. 사진 미사용. 수정 시 SVG 재생성 후 `cp svg/*.svg ../site/static/img/guide/`.
