#!/bin/bash
set +x
set -eu
umask 077
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
exec env -i PATH="$PATH" LANG=C.UTF-8 HANSET_SITE_ROOT="${HANSET_SITE_ROOT:-}" \
    HANSET_DATA_DIR="${HANSET_DATA_DIR:-}" HANSET_ACTIVE_DIR="${HANSET_ACTIVE_DIR:-}" \
    HANSET_TWIN_DIR="${HANSET_TWIN_DIR:-}" HANSET_IG_DIR="${HANSET_IG_DIR:-}" \
    HANSET_DEPLOY_CMD="${HANSET_DEPLOY_CMD:-}" HANSET_DEPLOY_LAUNCHER="${HANSET_DEPLOY_LAUNCHER:-}" \
    HANSET_QWEN_KEY_FILE=/dev/null HANSET_DATAGO_KEY_FILE=/dev/null \
    HANSET_RELEASE_PYTHON="${HANSET_RELEASE_PYTHON:-}" \
    python3 -I -B "$root/scripts/auto_publish.py" "$@"
