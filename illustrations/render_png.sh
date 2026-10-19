#!/bin/bash
cd /workspace/fitness-hub/illustrations
mkdir -p png
for f in svg/*.svg; do n=$(basename $f .svg)
 google-chrome --headless=new --no-sandbox --disable-gpu --hide-scrollbars --force-device-scale-factor=1 --window-size=1080,1350 --screenshot=png/$n.png "file://$PWD/$f" >/dev/null 2>&1
done
ls -la png
