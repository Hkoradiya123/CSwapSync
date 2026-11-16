#!/usr/bin/env bash
# Rollback to the snapshot taken automatically before the last restore. Add --yes to apply.
set -euo pipefail
umask 077
CS="$(command -v cswapsync)" || { echo "cswapsync not installed: uv tool install cswapsync" >&2; exit 127; }
exec "$CS" restore --latest-rollback "$@"
