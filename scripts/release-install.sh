#!/bin/bash
# install.sh, shipped inside the Local TTS release tarball.
#
#   ./install.sh                 # install to ~/.local
#   ./install.sh --prefix DIR    # install somewhere else
#
# Layout under the prefix:
#   bin/pocket-tts, bin/local-tts      small launchers on your PATH
#   libexec/local-tts/                 the real binary, libonnxruntime, licenses
#   share/local-tts/models, voices     weights and voices the launchers use
#
# Model weights are not in the tarball (CC BY 4.0). Run ./tools/prepare_models.sh
# first, then this script copies them. Run this script again after preparing.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="$HOME/.local"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="${2:?--prefix needs a directory}"; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "install: unknown option $1" >&2; exit 2 ;;
  esac
done

LIBEXEC="$PREFIX/libexec/local-tts"
SHARE="$PREFIX/share/local-tts"
BIN="$PREFIX/bin"
mkdir -p "$LIBEXEC" "$SHARE/voices" "$BIN"

# Runtime: the binary finds libonnxruntime next to itself, so copy them together.
cp "$HERE/pocket-tts" "$HERE"/libonnxruntime*.dylib "$LIBEXEC/"
cp "$HERE/local-tts" "$LIBEXEC/local-tts-client"
cp "$HERE/LICENSE" "$HERE/THIRD_PARTY_NOTICES.md" "$LIBEXEC/"
mkdir -p "$LIBEXEC/third_party_licenses"
cp -R "$HERE/third_party_licenses/." "$LIBEXEC/third_party_licenses/"
cp "$HERE"/voices/*.wav "$SHARE/voices/"

# Launchers. Defaults come first, so any flag you pass overrides them.
cat > "$BIN/pocket-tts" <<EOF
#!/bin/bash
exec "$LIBEXEC/pocket-tts" \\
  --models-dir "$SHARE/models" \\
  --voices-dir "$SHARE/voices" \\
  --tokenizer "$SHARE/models/tokenizer.model" \\
  "\$@"
EOF
cat > "$BIN/local-tts" <<EOF
#!/bin/bash
export LOCAL_TTS_VOICES_DIR="\${LOCAL_TTS_VOICES_DIR:-$SHARE/voices}"
exec "$LIBEXEC/local-tts-client" "\$@"
EOF
chmod 755 "$BIN/pocket-tts" "$BIN/local-tts"

# Weights, when the user already ran prepare_models.sh in this folder.
if [[ -f "$HERE/models/tokenizer.model" ]]; then
  mkdir -p "$SHARE/models"
  rsync -a --exclude '*.bak' --exclude '*.tmp' "$HERE/models/" "$SHARE/models/"
  echo "Models copied to $SHARE/models"
else
  echo
  echo "No model weights yet. Get them (one time, about 165 MB, CC BY 4.0):"
  echo "  $HERE/tools/prepare_models.sh"
  echo "Then run this installer again:"
  echo "  $HERE/install.sh"
fi

echo
echo "Installed to $PREFIX"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "Add $BIN to your PATH:  export PATH=\"$BIN:\$PATH\"" ;;
esac
echo "Speak:   pocket-tts \"Hello from Local TTS.\" example.wav hello.wav"
echo "Server:  pocket-tts --server --port 8081   (then: local-tts \"Hello.\" --play)"
