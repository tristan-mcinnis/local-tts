#!/usr/bin/env bash
# Regenerate the webdemo test fixtures deterministically (temperature 0) from
# the web model set and the shipped preset voice:
#   test/xfer_states.bin + xfer_meta.json   (gen_state_xfer.py)
#   test/ref_latents.f32 + ref_audio.f32 + ref_meta.json (gen_reference.py)
# Needs: tools/prepare_models.sh --web (model weights are not in git), uv.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
web="$(dirname "$here")"
root="$(dirname "$web")"

for f in flow_lm_main_delta_flow_int8.onnx mimi_decoder_delta_int8.onnx text_conditioner.onnx tokenizer.model; do
  if [ ! -f "$web/models/$f" ]; then
    echo "missing $web/models/$f; run ./tools/prepare_models.sh --web first" >&2
    exit 2
  fi
done
command -v uv >/dev/null || { echo "uv not found on PATH" >&2; exit 2; }

cd "$root"
uv run --no-project --with onnx --with onnxruntime --with sentencepiece --with numpy \
  python webdemo/test/gen_state_xfer.py
uv run --no-project --with onnx --with onnxruntime --with sentencepiece --with numpy \
  python webdemo/test/gen_reference.py
echo "fixtures written to $here"
