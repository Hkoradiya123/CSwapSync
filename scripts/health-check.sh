#!/usr/bin/env bash
# Health check incl. live CSwap account health (network).
set -euo pipefail
umask 077
CS="$(command -v cswapsync)" || { echo "cswapsync not installed: uv tool install cswapsync" >&2; exit 127; }
exec "$CS" doctor --health "$@"
