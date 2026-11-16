#!/usr/bin/env bash
# Install (or upgrade) the cswapsync tool, then run the interactive wizard.
#   scripts/setup.sh              from this checkout (dev / before PyPI)
#   scripts/setup.sh --pypi       from PyPI (same as: uv tool install cswapsync)
#   scripts/setup.sh --no-wizard  install only
# Data (keys, config, backups) stays in ~/CSwapSync (or $CSWAPSYNC_HOME).
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT"; WIZARD=1
for arg in "$@"; do
  case "$arg" in
    --pypi) SRC="cswapsync" ;;
    --no-wizard) WIZARD=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
if command -v uv >/dev/null; then
  uv tool install --force --python 3.12 "$SRC"
elif command -v pipx >/dev/null; then
  pipx install --force "$SRC"
else
  echo "need uv (https://docs.astral.sh/uv/) or pipx" >&2; exit 1
fi
if [ "$WIZARD" = 1 ]; then exec cswapsync setup; fi
