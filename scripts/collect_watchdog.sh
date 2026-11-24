#!/usr/bin/env bash
set -eu
set +x
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export PYTHONDONTWRITEBYTECODE=1
exec python3 -m hanset_active.collect.scheduled watchdog "$@"
