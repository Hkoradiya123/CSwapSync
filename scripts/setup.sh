#!/usr/bin/env bash
# CSwapSync setup (Ubuntu). Idempotent: re-running never overwrites existing
# keys, config, tokens or foreign files.
#
#   scripts/setup.sh                 venv + deps + config + keys + launcher + systemd units
#   scripts/setup.sh --no-systemd    skip systemd integration
#   scripts/setup.sh --gate-refresh  ALSO install the optional lease gate drop-in on
#                                    the existing cswap-refresh.service
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
SYSTEMD=1
GATE=0
for arg in "$@"; do
  case "$arg" in
    --no-systemd) SYSTEMD=0 ;;
    --gate-refresh) GATE=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '==> %s\n' "$*"; }

say "directories (0700)"
for d in config credentials staging logs backups; do mkdir -p "$ROOT/$d"; chmod 700 "$ROOT/$d"; done

say "python venv + dependencies"
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  if command -v uv >/dev/null; then uv venv -q "$ROOT/.venv" --python 3.12
  else python3 -m venv "$ROOT/.venv"; fi
fi
if command -v uv >/dev/null; then
  uv pip install -q --python "$ROOT/.venv/bin/python" -r "$ROOT/requirements.txt"
else
  "$ROOT/.venv/bin/python" -m pip install -q -r "$ROOT/requirements.txt"
fi

say "config"
[ -f "$ROOT/config/config.json" ] || cp "$ROOT/config/config.example.json" "$ROOT/config/config.json"
chmod 600 "$ROOT/config/config.json"
[ -f "$ROOT/config/device.json" ] || { echo "config/device.json missing" >&2; exit 2; }

say "device keys"
if [ -f "$ROOT/credentials/age-identity.txt" ]; then
  echo "    keys already exist (kept)"
else
  "$ROOT/scripts/cswapsync" keys init >/dev/null
fi
chmod 600 "$ROOT"/credentials/*.txt "$ROOT"/credentials/*.pem 2>/dev/null || true
[ -f "$ROOT/credentials/google-client-secret.json" ] && chmod 600 "$ROOT/credentials/google-client-secret.json"

say "launcher ~/.local/bin/cswapsync"
mkdir -p "$HOME/.local/bin"
LINK="$HOME/.local/bin/cswapsync"
if [ -L "$LINK" ] && [ "$(readlink -f "$LINK")" = "$ROOT/scripts/cswapsync" ]; then
  echo "    already linked"
elif [ -e "$LINK" ]; then
  echo "    $LINK exists and is not ours; NOT touching it" >&2
else
  ln -s "$ROOT/scripts/cswapsync" "$LINK"
fi

if [ "$SYSTEMD" = 1 ]; then
  say "systemd user units"
  mkdir -p "$UNIT_DIR"
  if [ ! -f "$UNIT_DIR/cswap-refresh.service" ]; then
    echo "    WARNING: existing cswap-refresh.service not found; post-refresh hook not wired" >&2
  fi
  for unit in cswapsync-heartbeat.service cswapsync-heartbeat.timer; do
    src="$ROOT/scripts/systemd/$unit"; dst="$UNIT_DIR/$unit"
    if [ -f "$dst" ] && ! cmp -s "$src" "$dst"; then
      echo "    $dst differs from project copy; NOT overwriting (diff it manually)" >&2
      continue
    fi
    install -m 644 "$src" "$dst"
  done
  if [ -f "$UNIT_DIR/cswap-refresh.service" ]; then
    # Sync runs INSIDE the existing refresh unit (ExecStartPost drop-in); the
    # original unit file is never edited.
    mkdir -p "$UNIT_DIR/cswap-refresh.service.d"
    install -m 644 "$ROOT/scripts/systemd/20-cswapsync-sync.conf" \
      "$UNIT_DIR/cswap-refresh.service.d/20-cswapsync-sync.conf"
  fi
  # Remove the old separate post-refresh unit from earlier installs.
  if [ -f "$UNIT_DIR/cswapsync-postrefresh.service" ]; then
    systemctl --user disable cswapsync-postrefresh.service 2>/dev/null || true
    rm -f "$UNIT_DIR/cswapsync-postrefresh.service"
  fi
  if [ "$GATE" = 1 ]; then
    mkdir -p "$UNIT_DIR/cswap-refresh.service.d"
    install -m 644 "$ROOT/scripts/systemd/10-cswapsync-lease-gate.conf" \
      "$UNIT_DIR/cswap-refresh.service.d/10-cswapsync-lease-gate.conf"
    echo "    lease gate installed on cswap-refresh.service"
  fi
  systemctl --user daemon-reload
  systemctl --user enable --now cswapsync-heartbeat.timer
fi

cat <<EOF

Setup done. Remaining manual steps:
  1. Put the Google OAuth Desktop client JSON at:
       $ROOT/credentials/google-client-secret.json   (chmod 600)
  2. cswapsync auth          # one-time browser consent, scope drive.file
  3. cswapsync keys show     # give this public bundle to the HP (one-time, out of band)
  4. cswapsync trust '<HP bundle>'
  5. cswapsync doctor
  6. First upload:  cswapsync pull  &&  cswapsync push
EOF
