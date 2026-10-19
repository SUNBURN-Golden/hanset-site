#!/bin/bash
cd /workspace/fitness-hub/illustrations
for f in svg/archive-*.svg; do n=$(basename $f .svg)
 google-chrome --headless=new --no-sandbox --disable-gpu --hide-scrollbars --force-device-scale-factor=1 --window-size=1080,1350 --virtual-time-budget=8000 --screenshot=png/$n.png "file://$PWD/$f" >/dev/null 2>&1
done
ls -la png | grep archive
