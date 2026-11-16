#!/usr/bin/env bash
# Session handoff. 'sync.sh pre [--yes]' before using Claude on this device,
# 'sync.sh post' after you are done (pushes and releases the lease).
set -euo pipefail
umask 077
CS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/cswapsync"
case "${1:-}" in
  pre)  shift; exec "$CS" pull "$@" ;;
  post) shift; exec "$CS" push "$@" ;;
  *) echo "usage: sync.sh pre [--yes] | post [--keep-lease]" >&2; exit 2 ;;
esac
