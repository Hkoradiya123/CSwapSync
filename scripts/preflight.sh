#!/usr/bin/env bash
# Preflight before a session: full diagnostics, then sync status.
set -euo pipefail
umask 077
CS="$(command -v cswapsync)" || { echo "cswapsync not installed: uv tool install cswapsync" >&2; exit 127; }
"$CS" doctor "$@"
exec "$CS" status
