# Map visibility diagnosis (2026-11-03 KST)
Live check of / and /finder/ at 1280 and 390, light and dark (before: /workspace/map-1103/before/*.png).
- No technical failure: the map is an inline SVG with no tiles or API. It has a non-zero height (561px at desktop, 429px at mobile) and all 49 pins render. The only console error is the Cloudflare Insights beacon being blocked by CSP, which is unrelated.
- Cause: the default "All Korea" view lays out all 49 pins as one uniform 7-column grid. Only one region label shows above it and there are no boundaries, so it reads as a wall of icons rather than a map. Region labels (Gyeonggi, Busan, Jeju) float with no visible region.
- Fix (minimal): in the All Korea view only, draw a dashed region zone around each region's pin block (`map_zones` in build_site.py, `.map-zone` CSS). Test: tests/test_explorer.py::AllKoreaZones.
After (local): /workspace/map-1103/local/*.png. After (live): /workspace/map-1103/after/*.png
