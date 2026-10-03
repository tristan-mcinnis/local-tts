#!/bin/bash
# Verify a Local TTS release tarball without touching your install.
#
#   ./scripts/verify-release.sh dist/local-tts-<version>-macos-arm64.tar.gz
#   MODELS_DIR=/path/to/models ./scripts/verify-release.sh <tarball>
#
# Extracts into a scratch folder, checks the contents, the ad hoc signatures,
# the architecture and the version in the file name, runs the binary from the
# extracted copy, tests install.sh into a scratch prefix, and (when weights are
# available) renders one short WAV. Nothing outside the scratch folder changes.
# The app is not notarized, so spctl rejects it: that is expected and recorded.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TARBALL="$(cd "$(dirname "${1:?usage: verify-release.sh <tarball>}")" && pwd)/$(basename "$1")"
MODELS_DIR="${MODELS_DIR:-$REPO/models}"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok: $*"; }

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

base="$(basename "$TARBALL" .tar.gz)"
[[ "$base" =~ ^local-tts-([0-9][0-9.]*)-macos-arm64$ ]] || fail "unexpected file name $base"
VERSION="${BASH_REMATCH[1]}"
cmake_version="$(sed -n 's/^project(pocket_tts VERSION \([0-9][0-9.]*\).*/\1/p' "$REPO/CMakeLists.txt" | head -n1)"
[[ "$VERSION" == "$cmake_version" ]] || fail "file name version $VERSION, CMakeLists version $cmake_version"
ok "version $VERSION matches CMakeLists.txt"

# Checksum, when SHA256SUMS sits next to the tarball.
sums="$(dirname "$TARBALL")/SHA256SUMS"
if [[ -f "$sums" ]]; then
  ( cd "$(dirname "$TARBALL")" && shasum -a 256 -c SHA256SUMS ) || fail "checksum mismatch"
fi

tar -xzf "$TARBALL" -C "$SCRATCH"
D="$SCRATCH/$base"
[[ -d "$D" ]] || fail "tarball does not extract to $base/"

for f in pocket-tts libonnxruntime.1.23.2.dylib local-tts install.sh README.txt LICENSE \
         THIRD_PARTY_NOTICES.md voices/example.wav tools/prepare_models.sh \
         third_party_licenses/onnxruntime/LICENSE third_party_licenses/onnxruntime/ThirdPartyNotices.txt; do
  [[ -s "$D/$f" ]] || fail "missing $f"
done
ls "$D"/tools/make_*.py >/dev/null || fail "missing tools/make_*.py"
for f in $(grep -o 'tools/make_[a-z_]*\.py' "$D/tools/prepare_models.sh" | sort -u); do
  [[ -s "$D/$f" ]] || fail "prepare_models.sh needs $f, not in the tarball"
done
[[ -x "$D/pocket-tts" && -x "$D/install.sh" ]] || fail "pocket-tts or install.sh is not executable"
ok "contents present (binary, dylib, client, voice, prep scripts, notices, ONNX Runtime licenses)"

# No weights, no personal files.
found="$(find "$D" \( -name '*.onnx' -o -name '.cache' -o -name '*.emb' -o -name '*.kv' \))"
[[ -z "$found" ]] || fail "weights or caches found in the tarball"
[[ "$(ls "$D/voices")" == "example.wav" ]] || fail "voices/ should hold only example.wav"
found="$(find "$D" \( -name '._*' -o -name '.DS_Store' \))"
[[ -z "$found" ]] || fail "stray metadata files"
ok "no weights, caches or stray files"

# Signatures, architecture, deployment target.
for f in libonnxruntime.1.23.2.dylib pocket-tts; do
  codesign --verify --strict --verbose=2 "$D/$f" 2>&1 | sed 's/^/  /'
  sig="$(codesign -dv "$D/$f" 2>&1)"
  [[ "$sig" == *"Signature=adhoc"* ]] || fail "$f is not ad hoc signed"
  [[ "$(lipo -archs "$D/$f")" == "arm64" ]] || fail "$f archs: $(lipo -archs "$D/$f")"
done
ok "codesign --verify --strict passes on the dylib and the binary (ad hoc); both arm64"
minos="$(vtool -show-build "$D/pocket-tts" | awk '/minos/ {print $2}')"
echo "  pocket-tts minos: $minos"
[[ "${minos%%.*}" -le 14 ]] || fail "deployment target $minos is too new for a release"
ok "deployment target $minos"

echo "spctl --assess (rejection expected, not notarized):"
spctl --assess --type execute --verbose=4 "$D/pocket-tts" 2>&1 | sed 's/^/  /' || true

# Run from the extracted copy: it must find the dylib next to itself.
help="$("$D/pocket-tts" --help 2>&1 || true)"
[[ "$help" == Usage:* ]] || fail "pocket-tts --help did not print usage"
echo "  ${help%%$'\n'*}"
ok "pocket-tts --help runs from the extracted copy"

# install.sh into a scratch prefix.
PREFIX="$SCRATCH/prefix"
"$D/install.sh" --prefix "$PREFIX" >"$SCRATCH/install.log" 2>&1 || { cat "$SCRATCH/install.log"; fail "install.sh"; }
for f in bin/pocket-tts bin/local-tts libexec/local-tts/pocket-tts libexec/local-tts/libonnxruntime.1.23.2.dylib \
         libexec/local-tts/LICENSE libexec/local-tts/THIRD_PARTY_NOTICES.md share/local-tts/voices/example.wav; do
  [[ -e "$PREFIX/$f" ]] || fail "install.sh did not create $f"
done
[[ "$(cat "$SCRATCH/install.log")" == *prepare_models.sh* ]] || fail "install.sh did not mention prepare_models.sh"
help="$("$PREFIX/bin/local-tts" --help 2>&1 || true)"
[[ "$help" == local-tts* ]] || fail "installed local-tts --help"
echo "  ${help%%$'\n'*}"
ok "install.sh works into a scratch prefix and points to prepare_models.sh"

# One real render, when weights are available.
if [[ -f "$MODELS_DIR/tokenizer.model" ]]; then
  out="$SCRATCH/out.wav"
  "$PREFIX/bin/pocket-tts" --temperature 0 --no-cache --models-dir "$MODELS_DIR" \
    --tokenizer "$MODELS_DIR/tokenizer.model" \
    "The release check speaks one short sentence." example.wav "$out" >"$SCRATCH/render.log" 2>&1 \
    || { tail -n 20 "$SCRATCH/render.log"; fail "synthesis from the installed copy"; }
  size="$(stat -f %z "$out")"
  [[ "$(head -c 4 "$out")" == "RIFF" && "$size" -gt 44 ]] || fail "output is not a WAV"
  ok "synthesized a WAV from the installed copy ($size bytes)"
else
  echo "skip: no weights at $MODELS_DIR, synthesis not tested"
fi
echo "VERIFY OK: $base"
