#!/usr/bin/env bash
# HANSET release: build --release -> pytest -> leak check -> wrangler deploy.
# Any failure aborts before deploy. Run from anywhere: scripts/deploy.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/site/.venv/bin/python"
DIST="$ROOT/site/dist"
LOG="${TMPDIR:-/tmp}/hanset-deploy-$(date +%Y%m%d-%H%M%S).log"

echo "== build --release"; (cd "$ROOT/site" && "$PY" build_site.py --release) 2>&1 | tee "$LOG"
echo "== pytest (log: $LOG)"
(cd "$ROOT" && "$PY" -m pytest tests -q -p no:cacheprovider) 2>&1 | tee -a "$LOG"
# Tests may rebuild site/dist; rebuild the release output for the leak check and deploy.
echo "== rebuild + leak check"
(cd "$ROOT/site" && "$PY" build_site.py --release) 2>&1 | tee -a "$LOG"
"$PY" - "$DIST" <<'PY'
import sys; sys.path.insert(0, sys.argv[1] + '/..')
import build_site as B; from pathlib import Path
bad = B.leak_check(Path(sys.argv[1]))
if bad: sys.exit(f"ABORT: leak check: {bad}")
print("leak check OK (standalone)")
PY
echo "== deploy"
mkdir -p /tmp/wr && cd /tmp/wr
CLOUDFLARE_ACCOUNT_ID=aa6bf084982ee841c3ea39b45895a15e npx wrangler@3 pages deploy "$DIST" --project-name hanset --branch main 2>&1 | tee -a "$LOG"
