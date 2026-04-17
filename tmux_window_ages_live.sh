#!/bin/sh

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOCKET="${1:-gt-be7f79}"
WATCH_PATH="${2:-$SCRIPT_DIR/tmux_window_ages.py}"

while true; do
  uv run "$SCRIPT_DIR/tmux_window_ages.py" "$SOCKET" &
  pid=$!

  fswatch -1 "$WATCH_PATH"

  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  clear
done
