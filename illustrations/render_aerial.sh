#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "$0")" && pwd)
python=${PYTHON:-python3}
chrome=${CHROME:-google-chrome}
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT

"$python" "$root/make_aerial.py" "$tmp"
scenes=("$@")
if (( ${#scenes[@]} == 0 )); then
    for svg in "$tmp"/aerial-*.svg; do
        scenes+=("$(basename -- "$svg" .svg)")
    done
fi

for scene in "${scenes[@]}"; do
    [[ $scene =~ ^aerial-[a-z-]+$ && -f "$tmp/$scene.svg" ]] || {
        echo "Unknown aerial scene: $scene" >&2
        exit 1
    }
    for variant in thumb mid; do
        if [[ $variant == thumb ]]; then
            width=252 height=315
        else
            width=720 height=900
        fi
        cat > "$tmp/render.html" <<HTML
<!doctype html><style>body{margin:0}img{display:block;width:${width}px;height:${height}px}</style>
<img src="$scene.svg" alt="">
HTML
        png="$tmp/$scene-$variant.png"
        "$chrome" --headless=new --no-sandbox --disable-gpu --hide-scrollbars \
            --disable-background-networking --disable-component-update --no-first-run \
            --force-device-scale-factor=1 --window-size="$width,$height" --virtual-time-budget=8000 \
            --screenshot="$png" "file://$tmp/render.html" >"$tmp/chrome.log" 2>&1 || {
            cat "$tmp/chrome.log" >&2
            exit 1
        }
        "$python" - "$png" "${png%.png}.webp" "$width" "$height" <<'PY'
import sys
from PIL import Image

png, webp, width, height = sys.argv[1:]
with Image.open(png) as image:
    w, h = int(width), int(height)
    if image.width < w or image.height < h:
        raise ValueError(f"Chrome viewport {image.size} is smaller than {(w, h)}")
    image.crop((0, 0, w, h)).save(webp, "WEBP", quality=90)
PY
    done
done

for scene in "${scenes[@]}"; do
    cp -- "$tmp/$scene.svg" "$root/svg/"
    cp -- "$tmp/$scene.svg" "$tmp/$scene-thumb.webp" "$tmp/$scene-mid.webp" "$root/../site/static/img/aerial/"
done
