#!/usr/bin/env bash
# HANSET twin daemon cron wrapper. Local only; no network, no credentials, no deploy.
set -u
repo_root=$(git -C "$(dirname -- "$0")" rev-parse --show-toplevel) || exit 1
cd -- "$repo_root" || exit 1
twin_dir=${HANSET_TWIN_DIR:-/workspace/hanset-data/twin}
mkdir -p -- "$twin_dir/logs" || exit 1
log="$twin_dir/logs/$(TZ=Asia/Seoul date +%Y-%m-%d).log"
{ printf '%s run_start\n' "$(TZ=Asia/Seoul date +%FT%T%z)"
  timeout 20m python3 -m hanset_twin run --twin-dir "$twin_dir" 2>&1
  printf '%s run_exit=%s\n' "$(TZ=Asia/Seoul date +%FT%T%z)" "$?"; } >> "$log"
