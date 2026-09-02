#!/bin/bash
# Install or refresh the Local TTS launchd agent from the checked-in plist.
# The plist is the single source of truth for label, port, and paths.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.tristan.local-tts"
SRC="$HERE/$LABEL.plist"
DST="$HOME/Library/LaunchAgents/$LABEL.plist"
[[ -x "$HERE/../pocket-tts" ]] || { echo "install: build first (cmake -B .build ... && cmake --build .build)" >&2; exit 1; }
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cp "$SRC" "$DST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DST"
echo "installed $LABEL; check: curl -s http://127.0.0.1:8081/health"
