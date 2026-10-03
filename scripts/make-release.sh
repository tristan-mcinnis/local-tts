#!/bin/bash
# Package Local TTS as a macOS arm64 tarball for a GitHub release.
#
#   ./scripts/make-release.sh                  # writes to ./dist
#   OUT_DIR=/some/dir ./scripts/make-release.sh
#
# Outputs in OUT_DIR:
#   local-tts-<version>-macos-arm64.tar.gz
#   SHA256SUMS
#   RELEASE_NOTES.md
#
# The tarball holds the pocket-tts binary, libonnxruntime (arm64 only), the
# local-tts client, an example voice, the model-prep scripts, install.sh and
# all license files. Model weights are not included (CC BY 4.0, downloaded by
# prepare_models.sh). Binary and dylib are signed ad hoc (identity "-"), inside
# out, with no --deep. The app is not notarized: there is no Apple Developer ID.
#
# It builds the runtime itself, in .build-release/ (arm64, macOS 13.4 floor), and
# leaves the repo-root binary alone. Needs CMake 3.28+ and Xcode command line tools.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

OUT_DIR="${OUT_DIR:-$REPO/dist}"
NOTES_NEW="${NOTES_NEW:-First prebuilt release for Apple Silicon.}"
ORT_DYLIB="libonnxruntime.1.23.2.dylib"
SLUG="tristan-mcinnis/local-tts"

die() { echo "make-release: $*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || die "macOS only"

# Version: the CMake project version is the single source.
VERSION="$(sed -n 's/^project(pocket_tts VERSION \([0-9][0-9.]*\).*/\1/p' CMakeLists.txt | head -n1)"
[[ -n "$VERSION" ]] || die "could not read the version from CMakeLists.txt"

NAME="local-tts-${VERSION}-macos-arm64"
TARBALL="$NAME.tar.gz"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Build the release binary in its own build tree and output folder, so the
# repo-root binary (a local service copy may come from it) is never touched.
# The deployment target is pinned to ONNX Runtime's own floor (13.4). Without
# it the compiler uses the host SDK (macOS 26 here) and the binary refuses to
# start on older macOS. Dependencies are pinned by hash or tag in CMakeLists.txt;
# when an earlier build already fetched them, reuse that copy and stay offline.
BUILD_DIR="$REPO/.build-release"
RUNTIME_OUT="$WORK/runtime"
CMAKE_ARGS=(-DCMAKE_BUILD_TYPE=Release -DCMAKE_OSX_ARCHITECTURES=arm64
            -DCMAKE_OSX_DEPLOYMENT_TARGET=13.4 -DPTT_OUTPUT_DIR="$RUNTIME_OUT")
if [[ -d .build/_deps/onnxruntime-src && -d .build/_deps/sentencepiece-src && -d .build/_deps/dr_libs-src ]]; then
  CMAKE_ARGS+=(-DFETCHCONTENT_FULLY_DISCONNECTED=ON
    -DFETCHCONTENT_SOURCE_DIR_ONNXRUNTIME="$REPO/.build/_deps/onnxruntime-src"
    -DFETCHCONTENT_SOURCE_DIR_SENTENCEPIECE="$REPO/.build/_deps/sentencepiece-src"
    -DFETCHCONTENT_SOURCE_DIR_DR_LIBS="$REPO/.build/_deps/dr_libs-src")
fi
cmake -S . -B "$BUILD_DIR" "${CMAKE_ARGS[@]}"
cmake --build "$BUILD_DIR" -j
[[ -x "$RUNTIME_OUT/pocket-tts" ]] || die "build did not produce pocket-tts"
[[ -f "$RUNTIME_OUT/$ORT_DYLIB" ]] || die "build did not produce $ORT_DYLIB"

# ONNX Runtime license files: the build copies them next to the binary.
ORT_LIC_DIR=""
for d in "$RUNTIME_OUT/third_party_licenses/onnxruntime" third_party_licenses/onnxruntime .build/_deps/onnxruntime-src; do
  if [[ -f "$d/LICENSE" && -f "$d/ThirdPartyNotices.txt" ]]; then ORT_LIC_DIR="$d"; break; fi
done
[[ -n "$ORT_LIC_DIR" ]] || die "ONNX Runtime license files not found"
[[ -f LICENSE && -f THIRD_PARTY_NOTICES.md ]] || die "LICENSE or THIRD_PARTY_NOTICES.md missing"

STAGE="$WORK/$NAME"
mkdir -p "$STAGE/tools" "$STAGE/voices" "$STAGE/third_party_licenses/onnxruntime" "$OUT_DIR"

# Runtime. The binary finds the dylib through @executable_path, so they stay
# side by side. Thin ONNX Runtime to arm64 (it ships universal2).
cp "$RUNTIME_OUT/pocket-tts" "$STAGE/pocket-tts"
if [[ "$(lipo -archs "$RUNTIME_OUT/$ORT_DYLIB")" == *x86_64* ]]; then
  lipo "$RUNTIME_OUT/$ORT_DYLIB" -thin arm64 -output "$STAGE/$ORT_DYLIB"
else
  cp "$RUNTIME_OUT/$ORT_DYLIB" "$STAGE/$ORT_DYLIB"
fi
[[ "$(lipo -archs "$STAGE/pocket-tts")" == "arm64" ]] || die "pocket-tts is not arm64 only"
[[ "$(lipo -archs "$STAGE/$ORT_DYLIB")" == "arm64" ]] || die "$ORT_DYLIB is not arm64 only"

# Ad hoc signing, inside out: the library first, then the binary that loads it.
codesign --force --sign - --timestamp=none "$STAGE/$ORT_DYLIB"
codesign --force --sign - --timestamp=none "$STAGE/pocket-tts"
codesign --verify --strict --verbose=2 "$STAGE/$ORT_DYLIB"
codesign --verify --strict --verbose=2 "$STAGE/pocket-tts"

# Client, voice, model-prep scripts, licenses, installer.
cp cli/local-tts "$STAGE/local-tts"
cp voices/example.wav "$STAGE/voices/example.wav"
cp tools/prepare_models.sh "$STAGE/tools/"
# prepare_models.sh runs these make_*.py scripts; ship exactly those it names.
for f in $(grep -o 'tools/make_[a-z_]*\.py' tools/prepare_models.sh | sort -u); do
  cp "$f" "$STAGE/tools/"
done
cp LICENSE THIRD_PARTY_NOTICES.md "$STAGE/"
cp "$ORT_LIC_DIR/LICENSE" "$ORT_LIC_DIR/ThirdPartyNotices.txt" "$STAGE/third_party_licenses/onnxruntime/"
cp scripts/release-install.sh "$STAGE/install.sh"
chmod 755 "$STAGE/install.sh" "$STAGE/local-tts" "$STAGE/tools/prepare_models.sh"

cat > "$STAGE/README.txt" <<EOF
Local TTS $VERSION (macOS, Apple Silicon)
Fast on-device voice cloning and text-to-speech. Nothing is uploaded.

1. If macOS blocks the files, run once after extracting:
     xattr -dr com.apple.quarantine "$NAME"
2. Get the model weights (one time, about 165 MB, CC BY 4.0):
     ./tools/prepare_models.sh
3. Install to ~/.local (or pass --prefix DIR):
     ./install.sh
4. Speak:
     pocket-tts "Hello from Local TTS." example.wav hello.wav

Source and docs: https://github.com/$SLUG
EOF

# Deterministic tarball: stable order, no extended attributes, fixed owner.
( cd "$WORK" && export COPYFILE_DISABLE=1 &&
  find "$NAME" | LC_ALL=C sort > list.txt &&
  tar --no-xattrs --uid 0 --gid 0 --numeric-owner -cf - -T list.txt --no-recursion | gzip -9n > "$OUT_DIR/$TARBALL" )

( cd "$OUT_DIR" && shasum -a 256 "$TARBALL" > SHA256SUMS )
SHA="$(cut -d' ' -f1 "$OUT_DIR/SHA256SUMS")"

cat > "$OUT_DIR/RELEASE_NOTES.md" <<EOF
# Local TTS $VERSION

Fast on-device voice cloning and text-to-speech, for Apple Silicon Macs. One C++ runtime around stock ONNX Runtime. It has a CLI, a localhost server with an OpenAI-compatible endpoint, and a thin \`local-tts\` client. Text and audio stay on your machine.

## What is new

$NOTES_NEW

## Requirements

- A Mac with Apple Silicon (arm64). macOS 13 or later.
- About 165 MB of disk for the model weights, downloaded once by \`tools/prepare_models.sh\`.
- \`curl\`, Python 3 and [uv](https://docs.astral.sh/uv/) to prepare the weights.

## Install

1. Download \`$TARBALL\` and \`SHA256SUMS\` from this page.
2. Check the download:

\`\`\`sh
shasum -a 256 -c SHA256SUMS
\`\`\`

3. Extract it, then remove the quarantine flag (see below):

\`\`\`sh
tar -xzf $TARBALL
xattr -dr com.apple.quarantine $NAME
cd $NAME
\`\`\`

4. Get the model weights (one time, CC BY 4.0), then install to \`~/.local\`:

\`\`\`sh
./tools/prepare_models.sh
./install.sh
pocket-tts "Hello from Local TTS." example.wav hello.wav
\`\`\`

\`install.sh\` puts \`pocket-tts\` and \`local-tts\` in \`~/.local/bin\`. Add that folder to your PATH if it is not there. Use \`./install.sh --prefix DIR\` to install somewhere else.

## First open

These files are not notarized. Local TTS is a free project, and it has no paid Apple Developer ID. So macOS may block the first run. Only use the files if you downloaded them from the Releases page of this repository. If macOS blocks a file, open System Settings, then Privacy & Security. Scroll down and click Open Anyway next to the blocked file. Confirm. Or remove the quarantine flag after you extract:

\`\`\`sh
xattr -dr com.apple.quarantine $NAME
\`\`\`

Each release is signed ad hoc. So macOS may ask again for permissions such as Accessibility or Microphone after an update. Grant them again when asked.

## Checksum

\`\`\`
$SHA  $TARBALL
\`\`\`

## Licenses

MIT for Local TTS. \`THIRD_PARTY_NOTICES.md\` and \`third_party_licenses/onnxruntime/\` are inside the tarball. The model weights and the example voice are CC BY 4.0 (Kyutai and contributors). Only clone voices you own or have consent to use.
EOF

echo
echo "Wrote:"
ls -l "$OUT_DIR/$TARBALL" "$OUT_DIR/SHA256SUMS" "$OUT_DIR/RELEASE_NOTES.md"
echo
echo "Next, verify:  ./scripts/verify-release.sh $OUT_DIR/$TARBALL"
echo "Then draft the release:"
echo "  gh release create v$VERSION --repo $SLUG --draft --title \"Local TTS $VERSION\" \\"
echo "    --notes-file $OUT_DIR/RELEASE_NOTES.md $OUT_DIR/$TARBALL $OUT_DIR/SHA256SUMS"
