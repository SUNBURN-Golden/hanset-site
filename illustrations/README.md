# 한셋 손그림 일러스트 (일반 안내 그림)
- 원본: `svg/*.svg` (1080×1350, 글자는 윤곽선 변환이라 폰트 없이 동일하게 보임). 같은 원본을 사이트(`site/static/img/guide/`)·인스타(PNG)·유튜브에 사용.
- PNG(1080×1350, 인스타 규격): `png/*.png`
- 생성: `python3 make_all.py && ./render_png.sh` (helper `handdraw.py`). 글꼴 Gaegu Bold (SIL OFL, `_fonts/OFL.txt`).
- 3종: crossfit-box-plan(박스 일반 구조), dropin-steps(드롭인 이용 순서), sauna-guide(사우나 순서·건식/습식 일반 구조)
- 규칙: 실제 업장의 구조를 그리지 않음(일반 안내 그림). 모든 그림에 "실제 업장과 다를 수 있어요" 표기. 사진 미사용. 수정 시 SVG 재생성 후 `cp svg/*.svg ../site/static/img/guide/`.

## Aerial drawing assets

Use Google Chrome and a temporary Python environment with FontTools and Pillow.
The script outlines Gaegu text, updates both SVG copies, and renders the
252 × 315 and 720 × 900 WebP variants used by the site.

```bash
aerial_env=$(mktemp -d)
python3 -m venv "$aerial_env"
"$aerial_env/bin/python" -m pip install fonttools Pillow
PYTHON="$aerial_env/bin/python" bash illustrations/render_aerial.sh
```

Pass a scene name such as `aerial-sisu-house` to update just that drawing.
This runs during illustration authoring. The site build copies the saved assets.
