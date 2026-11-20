#!/usr/bin/env bash
# Run discovery with its own overlap lock and external log.
set -u
set -o pipefail
set +x
umask 077

checkout=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P) || exit 1
active_dir=$(realpath -m -- "${HANSET_ACTIVE_DIR:-/workspace/hanset-data/active}") || exit 1
ig_dir=$(realpath -m -- "${HANSET_IG_DIR:-/workspace/hanset-data/ig}") || exit 1
for dir in "$active_dir" "$ig_dir"; do
    case "$dir/" in
        "$checkout/"*) exit 1 ;;
    esac
done
mkdir -p -- "$active_dir/logs" || exit 1
export HANSET_ACTIVE_DIR="$active_dir"
export HANSET_IG_DIR="$ig_dir"
export PYTHONDONTWRITEBYTECODE=1
cd -- "$checkout" || exit 1

log="$active_dir/logs/seeds-$(TZ=Asia/Seoul date +%F).log"
exec 9>"$active_dir/seed-discovery-cron.lock"
if ! flock -n 9; then
    printf '%s already_running\n' "$(TZ=Asia/Seoul date -Iseconds)" >>"$log"
    exit 0
fi

timeout --signal=TERM --kill-after=10s 110m \
    python3 -m hanset_active seeds discover "$@" >>"$log" 2>&1
result=$?
printf '%s seeds_exit=%s\n' "$(TZ=Asia/Seoul date -Iseconds)" "$result" >>"$log"
exit "$result"
