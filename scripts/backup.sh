#!/usr/bin/env bash
# Local-only encrypted backup of all CSwap accounts (no cloud, no lease).
set -euo pipefail
umask 077
CS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/cswapsync"
exec "$CS" backup "$@"
