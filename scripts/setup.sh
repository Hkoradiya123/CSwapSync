#!/usr/bin/env bash
# CSwapSync bootstrap (Linux): venv + deps + launcher, then the interactive wizard.
#   scripts/setup.sh                 bootstrap, then `cswapsync setup`
#   scripts/setup.sh --no-wizard     bootstrap only
#   scripts/setup.sh --gate-refresh  also skip cswap-refresh while another device holds the lease
# Idempotent: never overwrites keys, config, tokens or foreign files.
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WIZARD=1; GATE=0
for arg in "$@"; do
  case "$arg" in
    --no-wizard) WIZARD=0 ;;
    --gate-refresh) GATE=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

for d in config credentials staging logs backups; do mkdir -p "$ROOT/$d"; chmod 700 "$ROOT/$d"; done

echo "==> python venv + dependencies"
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  if command -v uv >/dev/null; then uv venv -q "$ROOT/.venv" --python 3.12
  else python3 -m venv "$ROOT/.venv"; fi
fi
if command -v uv >/dev/null; then
  uv pip install -q --python "$ROOT/.venv/bin/python" -r "$ROOT/requirements.txt"
else
  "$ROOT/.venv/bin/python" -m pip install -q -r "$ROOT/requirements.txt"
fi
chmod 700 "$ROOT/scripts/cswapsync" "$ROOT"/scripts/*.sh

if [ "$GATE" = 1 ]; then
  DIR="$HOME/.config/systemd/user/cswap-refresh.service.d"
  mkdir -p "$DIR"
  install -m 644 "$ROOT/scripts/systemd/10-cswapsync-lease-gate.conf" "$DIR/"
  systemctl --user daemon-reload
  echo "==> lease gate installed on cswap-refresh.service"
fi

if [ "$WIZARD" = 1 ]; then
  exec "$ROOT/scripts/cswapsync" setup
fi
