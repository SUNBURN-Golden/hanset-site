#!/usr/bin/env bash
# Web collection, or a desktop Instagram batch already sitting in the inbox.
# This wrapper never starts a browser, reads a session file, or prints credentials.
# It does not deploy the site. Instagram browsing stays on the approved desktop.
set -u
set -o pipefail
set +x
umask 077

mode=${1:-web}
case "$mode" in
    web|ingest) ;;
    *) exit 2 ;;
esac

checkout=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P) || exit 1
ig_dir=${HANSET_IG_DIR:-/workspace/hanset-data/ig}
active_dir=${HANSET_ACTIVE_DIR:-/workspace/hanset-data/active}
twin_dir=${HANSET_TWIN_DIR:-/workspace/hanset-data/twin}
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
exec 9>"$ig_dir/cron.lock"
if ! flock -n 9; then
    printf '%s already_running\n' "$(TZ=Asia/Seoul date -Iseconds)" >>"$log"
    exit 0
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
timeout --signal=TERM --kill-after=10s 25m \
    python3 -m hanset_active collect "$mode" >>"$log" 2>&1
result=$?
printf '%s %s_exit=%s\n' "$(TZ=Asia/Seoul date -Iseconds)" "$mode" "$result" >>"$log"
exit "$result"
