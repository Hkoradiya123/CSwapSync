#!/usr/bin/env bash
# Restore from a verified backup. Verify-only unless --yes. Examples:
#   restore.sh --cloud-revision 12        restore.sh --file backups/r000012-<id>.age --yes
set -euo pipefail
umask 077
CS="$(command -v cswapsync)" || { echo "cswapsync not installed: uv tool install cswapsync" >&2; exit 127; }
exec "$CS" restore "$@"
