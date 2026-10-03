#!/bin/sh
# Publish scrub: no user-specific paths, private notes or key material in
# tracked files.
#
# Generic patterns live here. Private tokens (client and colleague names,
# work email domains, hostnames) belong in .scrub-private (gitignored, one
# case-insensitive ERE per line) so the scanner never publishes what it scans
# for. This file is the one tracked file excluded from the scan; review it by
# hand.
#
# The private pass skips third-party files that are not ours to edit and that
# hold arbitrary English words: the Pocket TTS tokenizer vocabulary and the
# vendored lame.js / ONNX Runtime / WASM builds under webdemo/vendor/. The
# generic pass (paths and key shapes) scans every tracked file, binaries
# included; the private pass reads text files only (-I), since short names
# match random bytes in images and embeddings.
set -e
cd "$(dirname "$0")/.."

GENERIC='/Users/tristan|~/vault|~/memory|vault-vps|BEGIN (RSA|OPENSSH|EC|DSA)? ?PRIVATE KEY|sk-[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|hf_[A-Za-z0-9]{30,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}'
THIRD_PARTY='^(tests/scrub\.sh|webdemo/models/spm_vocab\.json|webdemo/vendor/.*)$'

PRIVATE=""
if [ -f .scrub-private ]; then
  PRIVATE=$(grep -v '^[[:space:]]*$' .scrub-private | grep -v '^#' | paste -sd'|' -)
fi

# Positive controls: prove each pass detects a planted hit.
control=$(mktemp)
printf '/Users/tristan/leak\n' > "$control"
if ! grep -qE "$GENERIC" "$control"; then
  echo "SCRUB_SELFTEST_FAILED (generic)"; rm -f "$control"; exit 1
fi
if [ -n "$PRIVATE" ]; then
  head -n1 .scrub-private | sed 's/\\b//g; s/\\//g; s/ ?/ /g; s/?//g' > "$control"
  if ! grep -qiE "$PRIVATE" "$control"; then
    echo "SCRUB_SELFTEST_FAILED (private)"; rm -f "$control"; exit 1
  fi
fi
rm -f "$control"

hits=$(git ls-files -z | grep -zv '^tests/scrub\.sh$' | xargs -0 grep -lE "$GENERIC" 2>/dev/null || true)
if [ -n "$PRIVATE" ]; then
  phits=$(git ls-files -z | grep -zvE "$THIRD_PARTY" | xargs -0 grep -IliE "$PRIVATE" 2>/dev/null || true)
  hits=$(printf '%s\n%s\n' "$hits" "$phits" | grep -v '^$' | sort -u || true)
else
  echo "note: no .scrub-private file; private-name pass skipped"
fi
if [ -n "$hits" ]; then
  echo "PRIVATE CONTENT FOUND:"
  echo "$hits"
  exit 1
fi
echo "SCRUB_CLEAN"
