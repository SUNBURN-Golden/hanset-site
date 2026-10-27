#!/usr/bin/env bash
# HANSET active bot cron wrapper. No deployment or credentials.
set -u
set -o pipefail
repo_root=$(git -C "$(dirname -- "$0")" rev-parse --show-toplevel) || exit 1
cd -- "$repo_root" || exit 1
active_dir=$(python3 -c 'from hanset_active.config import Settings; print(Settings().data_dir)') || exit 1
mkdir -p -- "$active_dir/logs" || exit 1
log_path="$active_dir/logs/$(TZ=Asia/Seoul date +%Y-%m-%d).log"
stamp() { TZ=Asia/Seoul date +%Y-%m-%dT%H:%M:%S%z; }
exec 9>"$active_dir/cron.lock"
if ! flock -n 9; then
    printf '%s already_running\n' "$(stamp)" >> "$log_path"
    exit 0
fi
printf '%s run_start\n' "$(stamp)" >> "$log_path"
python3 -m hanset_active run --max-minutes 25 2>&1 |
    while IFS= read -r line || [ -n "$line" ]; do
        printf '%s %s\n' "$(stamp)" "$line"
    done >> "$log_path"
run_exit=${PIPESTATUS[0]}
printf '%s run_exit=%s\n' "$(stamp)" "$run_exit" >> "$log_path"
exit "$run_exit"
