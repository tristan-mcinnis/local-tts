#!/bin/zsh
# Install the TTS runtime where launchd can read it without a Documents grant.
#
# launchd jobs get no Files-and-Folders consent dialog: a binary or dylib read
# from ~/Documents blocks the process silently, forever (2026-09-03, after the
# repo moved under ~/Documents). So the runtime the job executes is copied out
# of the repo, and the voices it reads live under ~/Models.
#
# The plist in launchd/ is a template: @HOME@ is replaced with $HOME here.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.tristan.local-tts"
RUNTIME="$HOME/.local/libexec/local-tts"
VOICES="$HOME/Models/pocket-tts/voices"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT=8081   # matches --port in the plist template
mkdir -p "$RUNTIME" "$VOICES" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cp "$REPO/pocket-tts" "$REPO"/libonnxruntime*.dylib "$REPO/libptt_custom_ops.dylib" "$RUNTIME/"
# Licenses travel with the copied binary and libonnxruntime.
cp "$REPO/LICENSE" "$REPO/THIRD_PARTY_NOTICES.md" "$RUNTIME/"
if [[ -d "$REPO/third_party_licenses" ]]; then
  rsync -a "$REPO/third_party_licenses/" "$RUNTIME/third_party_licenses/"
fi
rsync -a "$REPO/voices/" "$VOICES/"
# The service reads weights from ~/Models/pocket-tts. On a first install, copy
# the set tools/prepare_models.sh built into models/ (skipped when models/ is
# already a link to that store).
MODELS="$HOME/Models/pocket-tts"
if [[ ! -f "$MODELS/tokenizer.model" ]]; then
  [[ -f "$REPO/models/tokenizer.model" ]] || {
    echo "install: no model set; run ./tools/prepare_models.sh first" >&2; exit 1; }
  rsync -a --exclude '*.bak' --exclude '*.tmp' "$REPO/models/" "$MODELS/"
fi
sed -e "s|@HOME@|$HOME|g" "$REPO/launchd/$LABEL.plist.in" > "$PLIST"
plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
# bootout returns before the job is gone; bootstrapping too soon fails with
# "Bootstrap failed: 5" and leaves the service stopped. Wait, then retry once.
for _ in $(seq 1 20); do
  launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || break
  sleep 0.5
done
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null \
  || { sleep 2; launchctl bootstrap "gui/$(id -u)" "$PLIST"; }
for _ in $(seq 1 20); do
  curl -s --max-time 2 "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q ok && { echo "local-tts healthy"; exit 0; }
  sleep 2
done
echo "local-tts did not answer /health" >&2; exit 1
