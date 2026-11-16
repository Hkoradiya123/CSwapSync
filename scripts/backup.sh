#!/usr/bin/env bash
# Local-only encrypted backup of all CSwap accounts (no cloud, no lease).
set -euo pipefail
umask 077
CS="$(command -v cswapsync)" || { echo "cswapsync not installed: uv tool install cswapsync" >&2; exit 127; }
exec "$CS" backup "$@"
