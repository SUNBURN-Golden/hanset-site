#!/usr/bin/env bash
# Web collection, or a desktop Instagram batch already sitting in the inbox.
# This wrapper never starts a browser, reads a session file, or prints credentials.
# Instagram browsing stays on the approved desktop.
set -u
set -o pipefail
set +x
umask 077

mode=${1:-web}
data_dir=${HANSET_DATA_DIR:-/workspace/hanset-data}
case "$mode" in
    web|wide|ingest) ;;
    *) exit 2 ;;
esac

checkout=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P) || exit 1
ig_dir=${HANSET_IG_DIR:-"$data_dir/ig"}
active_dir=${HANSET_ACTIVE_DIR:-"$data_dir/active"}
twin_dir=${HANSET_TWIN_DIR:-"$data_dir/twin"}
ig_dir=$(realpath -m -- "$ig_dir") || exit 1
active_dir=$(realpath -m -- "$active_dir") || exit 1
twin_dir=$(realpath -m -- "$twin_dir") || exit 1
for dir in "$ig_dir" "$active_dir" "$twin_dir"; do
    case "$dir/" in
        "$checkout/"*) exit 1 ;;
    esac
done
mkdir -p -- "$ig_dir/logs" "$ig_dir/inbox" "$ig_dir/tmp" || exit 1
chmod 700 "$ig_dir" "$ig_dir/logs" "$ig_dir/inbox" "$ig_dir/tmp" || exit 1
export HANSET_IG_DIR="$ig_dir"
export HANSET_ACTIVE_DIR="$active_dir"
export HANSET_TWIN_DIR="$twin_dir"
export PYTHONPATH="$checkout${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
cd -- "$checkout" || exit 1
# A copied browser session must not become part of this process.
unset HANSET_IG_SESSION

log="$ig_dir/logs/collect-$(TZ=Asia/Seoul date +%F).log"
if [[ "$mode" == ingest ]]; then
    exec 9>"$ig_dir/cron.lock"
    if ! flock -n 9; then
        printf '%s already_running\n' "$(TZ=Asia/Seoul date -Iseconds)" >>"$log"
        exit 0
    fi
fi

env_file="$ig_dir/deepseek.env"
if [[ -e "$env_file" ]]; then
    if [[ -L "$env_file" ]]; then
        printf '%s credential_file_symlink\n' "$(TZ=Asia/Seoul date -Iseconds)" >>"$log"
        exit 1
    fi
    mode_bits=$(stat -c %a -- "$env_file") || exit 1
    if [[ "$mode_bits" != "600" || ! -O "$env_file" ]]; then
        printf '%s credential_file_permissions\n' "$(TZ=Asia/Seoul date -Iseconds)" >>"$log"
        exit 1
    fi
    set -a
    # shellcheck disable=SC1090
    . "$env_file" >/dev/null 2>&1 || {
        printf '%s credential_file_unreadable\n' "$(TZ=Asia/Seoul date -Iseconds)" >>"$log"
        exit 1
    }
    set +a
fi

printf '%s %s_start\n' "$(TZ=Asia/Seoul date -Iseconds)" "$mode" >>"$log"
case "$mode" in
    web) command=(python3 -m hanset_active.collect.scheduled daily_collect) ;;
    wide) command=(python3 -m hanset_active.collect.scheduled wide_collect) ;;
    ingest) command=(timeout --signal=TERM --kill-after=10s 25m python3 -m hanset_active collect ingest) ;;
esac
unset HANSET_RUN_TOKEN
if [[ "$mode" != ingest && "${HANSET_AUTO_PUBLISH:-}" == 1 && -z "${HANSET_CATCHUP_SLOT:-}" ]]; then
    read -r HANSET_RUN_TOKEN </proc/sys/kernel/random/uuid || exit 1
    export HANSET_RUN_TOKEN
fi
"${command[@]}" >>"$log" 2>&1
result=$?
printf '%s %s_exit=%s\n' "$(TZ=Asia/Seoul date -Iseconds)" "$mode" "$result" >>"$log"
if [[ "$result" == 0 && -n "${HANSET_RUN_TOKEN:-}" ]]; then
    job=${command[3]}
    if python3 - "$data_dir" "$job" "$HANSET_RUN_TOKEN" <<'PYTHON'
import re
import sys
from pathlib import Path
from hanset_active.collect.private_io import read_json

root, job, token = sys.argv[1:]
try:
    row = read_json(Path(root) / "stamps" / (job + ".publication.json"))
    matched = (row["schema_version"] == 1 and row["job"] == job and row["run_token"] == token
               and re.fullmatch(r"[0-9a-f]{64}", row["request_id"] or ""))
except (OSError, ValueError, KeyError, TypeError):
    matched = False
sys.exit(0 if matched else 1)
PYTHON
    then
        "$checkout/scripts/auto_publish.sh" --drain >>"$log" 2>&1 ||
            printf '%s publication_failed\n' "$(TZ=Asia/Seoul date -Iseconds)" >>"$log"
    fi
fi
exit "$result"
