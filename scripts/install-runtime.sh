#!/bin/zsh
# Install the TTS runtime where launchd can read it without a Documents grant.
#
# launchd jobs get no Files-and-Folders consent dialog: a binary or dylib read
# from ~/Documents blocks the process silently, forever (2026-09-03, after the
# repo moved into ~/Documents/code/house). So the runtime the job executes is
# copied out of the repo, and the voices it reads live under ~/Models.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME="$HOME/.local/libexec/local-tts"
VOICES="$HOME/Models/pocket-tts/voices"
PLIST="$HOME/Library/LaunchAgents/com.tristan.local-tts.plist"
mkdir -p "$RUNTIME" "$VOICES"
cp "$REPO/pocket-tts" "$REPO"/libonnxruntime*.dylib "$REPO/libptt_custom_ops.dylib" "$RUNTIME/"
rsync -a "$REPO/voices/" "$VOICES/"
cp "$REPO/launchd/com.tristan.local-tts.plist" "$PLIST"
launchctl bootout "gui/$(id -u)/com.tristan.local-tts" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
for _ in $(seq 1 20); do
  curl -s --max-time 2 http://127.0.0.1:8081/health 2>/dev/null | grep -q ok && { echo "local-tts healthy"; exit 0; }
  sleep 2
done
echo "local-tts did not answer /health" >&2; exit 1
