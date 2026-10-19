#!/bin/bash
# usage: render1.sh svgfile pngfile
google-chrome --headless=new --no-sandbox --disable-gpu --hide-scrollbars --force-device-scale-factor=1 --window-size=1080,1350 --virtual-time-budget=8000 --screenshot=$2 file://$1 >/dev/null 2>&1
