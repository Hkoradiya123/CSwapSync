#!/usr/bin/env bash
# Preflight before a session: full diagnostics, then sync status.
set -euo pipefail
umask 077
CS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/cswapsync"
"$CS" doctor "$@"
exec "$CS" status
