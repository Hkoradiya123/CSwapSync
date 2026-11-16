#!/bin/sh
# One-line installer (Linux/macOS):
#   curl -LsSf https://raw.githubusercontent.com/Hkoradiya123/CSwapSync/main/install.sh | sh
#   curl -LsSf .../install.sh | sh -s -- --use-index   # also enable bare `uv tool install cswapsync`
#   curl -LsSf .../install.sh | sh -s -- --no-setup    # install only
# Needs uv (preferred) or pipx. Never stores secrets; data lives in ~/CSwapSync.
set -eu
# zip archive instead of git+https: works without git installed
REPO="cswapsync @ https://github.com/Hkoradiya123/CSwapSync/archive/refs/heads/main.zip"
INDEX_URL="https://hkoradiya123.github.io/CSwapSync/simple/"
USE_INDEX=0; SETUP=1
for arg in "$@"; do
  case "$arg" in
    --use-index) USE_INDEX=1 ;;
    --no-setup) SETUP=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if command -v uv >/dev/null 2>&1; then
  uv tool install --force --python 3.12 "$REPO"
  BIN="$(uv tool dir --bin)/cswapsync"
elif command -v pipx >/dev/null 2>&1; then
  pipx install --force "$REPO"
  BIN="$(command -v cswapsync || echo "$HOME/.local/bin/cswapsync")"
else
  echo "Install uv first:  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

if [ "$USE_INDEX" = 1 ]; then
  CFG="${XDG_CONFIG_HOME:-$HOME/.config}/uv/uv.toml"
  mkdir -p "$(dirname "$CFG")"
  if grep -qs "$INDEX_URL" "$CFG"; then
    echo "uv.toml already has the cswapsync index"
  else
    printf '\n[[index]]\nname = "cswapsync"\nurl = "%s"\n' "$INDEX_URL" >> "$CFG"
    echo "added cswapsync index to $CFG (bare 'uv tool install cswapsync' now works)"
  fi
fi

command -v cswapsync >/dev/null 2>&1 || echo "note: add $(dirname "$BIN") to PATH (uv: 'uv tool update-shell')"
if [ "$SETUP" = 1 ]; then
  # stdin is this script when piped from curl; talk to the terminal instead.
  if [ -r /dev/tty ]; then exec "$BIN" setup </dev/tty; fi
  echo "no terminal: run 'cswapsync setup' yourself"
fi
