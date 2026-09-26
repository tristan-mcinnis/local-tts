#!/bin/bash
# Install or refresh the Local TTS launchd agent.
#
# The plist runs a copy of the binary in ~/.local/libexec/local-tts and reads
# voices from ~/Models/pocket-tts/voices, because a launchd job may not read
# ~/Documents. Loading the plist alone would rerun whatever copy is already
# there, so this hands off to the one install path that refreshes the copy,
# the voices and the plist together.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
[[ -x "$HERE/../pocket-tts" ]] || { echo "install: build first (cmake -B .build ... && cmake --build .build)" >&2; exit 1; }
exec "$HERE/../scripts/install-runtime.sh"
