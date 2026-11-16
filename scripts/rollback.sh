#!/usr/bin/env bash
# Rollback to the snapshot taken automatically before the last restore. Add --yes to apply.
set -euo pipefail
umask 077
CS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/cswapsync"
exec "$CS" restore --latest-rollback "$@"
