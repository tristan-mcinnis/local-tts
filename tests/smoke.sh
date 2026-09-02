#!/usr/bin/env bash
# Native smoke test for ctest: render one sentence from voices/example.wav at
# temperature 0 and check a WAV header comes out. Exit 77 (skipped) when the
# model bundle is not present; weights are never in git.
#   tests/smoke.sh <pocket-tts binary> <models dir> <voices dir>
set -u
bin="${1:?binary}"; models="${2:?models dir}"; voices="${3:?voices dir}"

if [ ! -f "$models/tokenizer.model" ] || ! ls "$models"/flow_lm_main*.onnx >/dev/null 2>&1; then
  echo "SKIP: no model bundle at $models (run tools/prepare_models.sh)"
  exit 77
fi
[ -x "$bin" ] || { echo "FAIL: binary not found at $bin"; exit 1; }
[ -f "$voices/example.wav" ] || { echo "FAIL: $voices/example.wav missing"; exit 1; }

out="$(mktemp -t ptt-smoke).wav"
trap 'rm -f "$out"' EXIT
if ! "$bin" --temperature 0 --no-cache --models-dir "$models" --tokenizer "$models/tokenizer.model" \
     --voices-dir "$voices" "The smoke test speaks one short sentence." example.wav "$out"; then
  echo "FAIL: pocket-tts exited non-zero"; exit 1
fi
size=$(stat -f %z "$out" 2>/dev/null || stat -c %s "$out")
magic=$(head -c 4 "$out"); fmt=$(dd if="$out" bs=1 skip=8 count=4 2>/dev/null)
if [ "$magic" = "RIFF" ] && [ "$fmt" = "WAVE" ] && [ "$size" -gt 44 ]; then
  echo "OK: WAV header present, $size bytes"; exit 0
fi
echo "FAIL: not a WAV (magic='$magic' fmt='$fmt' size=$size)"; exit 1
