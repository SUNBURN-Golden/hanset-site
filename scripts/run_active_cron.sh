#!/usr/bin/env bash
set -u
set -o pipefail
set +x
umask 077
checkout=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P) || exit 1
cd -- "$checkout" || exit 1
export PYTHONDONTWRITEBYTECODE=1
unset HANSET_IG_SESSION
data_dir=${HANSET_DATA_DIR:-/workspace/hanset-data}
log_dir=$(realpath -m -- "${HANSET_ACTIVE_DIR:-$data_dir/active}")/logs
case "$log_dir/" in
    "$checkout/"*) exit 1 ;;
esac
mkdir -p -- "$log_dir" || exit 1
log="$log_dir/$(TZ=Asia/Seoul date +%F).log"
python3 -m hanset_active.collect.scheduled active "$@" 2>&1 |
    while IFS= read -r line || [ -n "$line" ]; do
        printf '%s %s\n' "$(TZ=Asia/Seoul date +%FT%T%z)" "$line"
    done >>"$log"
result=${PIPESTATUS[0]}
if (( result != 0 )); then
    printf '%s active_exit=%s\n' "$(TZ=Asia/Seoul date -Iseconds)" "$result" >>"$log"
fi
exit "$result"
