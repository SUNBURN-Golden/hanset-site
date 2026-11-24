#!/usr/bin/env bash
set -eu
set +x
umask 077
checkout=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd -- "$checkout"
export PYTHONDONTWRITEBYTECODE=1
unset HANSET_IG_SESSION
data_dir=${HANSET_DATA_DIR:-/workspace/hanset-data}
active_dir=$(realpath -m -- "${HANSET_ACTIVE_DIR:-$data_dir/active}")
ig_dir=$(realpath -m -- "${HANSET_IG_DIR:-$data_dir/ig}")
for dir in "$active_dir" "$ig_dir"; do
    case "$dir/" in
        "$checkout/"*) exit 1 ;;
    esac
done
export HANSET_ACTIVE_DIR="$active_dir" HANSET_IG_DIR="$ig_dir"
mkdir -p -- "$active_dir/logs"
log="$active_dir/logs/seeds-$(TZ=Asia/Seoul date +%F).log"
result=0
if (( $# )); then
    exec 9>"$active_dir/seed-discovery-cron.lock"
    if ! flock -n 9; then
        printf '%s already_running\n' "$(TZ=Asia/Seoul date -Iseconds)" >>"$log"
        exit 0
    fi
    timeout --signal=TERM --kill-after=10s 110m \
        python3 -m hanset_active seeds discover "$@" >>"$log" 2>&1 || result=$?
else
    python3 -m hanset_active.collect.scheduled seed_discovery >>"$log" 2>&1 || result=$?
fi
printf '%s seeds_exit=%s\n' "$(TZ=Asia/Seoul date -Iseconds)" "$result" >>"$log"
exit "$result"
