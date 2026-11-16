#!/usr/bin/env bash
# Health check incl. live CSwap account health (network).
set -euo pipefail
umask 077
CS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/cswapsync"
exec "$CS" doctor --health "$@"
