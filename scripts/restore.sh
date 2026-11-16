#!/usr/bin/env bash
# Restore from a verified backup. Verify-only unless --yes. Examples:
#   restore.sh --cloud-revision 12        restore.sh --file backups/r000012-<id>.age --yes
set -euo pipefail
umask 077
CS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/cswapsync"
exec "$CS" restore "$@"
