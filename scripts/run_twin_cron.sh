#!/usr/bin/env bash
set -eu
set +x
umask 077
checkout=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd -- "$checkout"
export PYTHONDONTWRITEBYTECODE=1
unset HANSET_IG_SESSION
data_dir=${HANSET_DATA_DIR:-/workspace/hanset-data}
log_dir=$(realpath -m -- "${HANSET_TWIN_DIR:-$data_dir/twin}")/logs
case "$log_dir/" in
    "$checkout/"*) exit 1 ;;
esac
mkdir -p -- "$log_dir"
log="$log_dir/$(TZ=Asia/Seoul date +%F).log"
result=0
printf '%s run_start\n' "$(TZ=Asia/Seoul date +%FT%T%z)" >>"$log"
python3 -m hanset_active.collect.scheduled twin "$@" >>"$log" 2>&1 || result=$?
printf '%s run_exit=%s\n' "$(TZ=Asia/Seoul date -Iseconds)" "$result" >>"$log"
exit "$result"
