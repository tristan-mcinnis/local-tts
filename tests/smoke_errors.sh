#!/usr/bin/env bash
# Error-body regression test for ctest: start a `--server`, ask
# /v1/audio/speech for a voice whose name holds a double quote, and require the
# error reply to be valid JSON that carries the name back. Before the fix the
# exception text went into the body raw, so a quote broke the JSON. Exit 77
# (skipped) when the model bundle is absent; weights are never in git.
#   tests/smoke_errors.sh <pocket-tts binary> <models dir> <voices dir>
set -u
bin="${1:?binary}"; models="${2:?models dir}"; voices="${3:?voices dir}"

if [ ! -f "$models/tokenizer.model" ] || ! ls "$models"/flow_lm_main*.onnx >/dev/null 2>&1; then
  echo "SKIP: no model bundle at $models (run tools/prepare_models.sh)"
  exit 77
fi
[ -x "$bin" ] || { echo "FAIL: binary not found at $bin"; exit 1; }

# A fresh voices dir, so nothing is cached into the real one.
voices_tmp="$(mktemp -dt ptt-errors-voices.XXXXXX)"
cp "$voices/example.wav" "$voices_tmp/example.wav"
port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
log="$(mktemp -t ptt-errors.XXXXXX).log"
outpid=""
cleanup() {
  if [ -n "$outpid" ] && kill -0 "$outpid" 2>/dev/null; then
    kill -TERM "$outpid" 2>/dev/null
    for _ in $(seq 1 40); do kill -0 "$outpid" 2>/dev/null || break; sleep 0.1; done
    kill -KILL "$outpid" 2>/dev/null || true
  fi
  rm -rf "$voices_tmp" "$log"
}
trap cleanup EXIT

"$bin" --server --port "$port" --models-dir "$models" \
  --voices-dir "$voices_tmp" --tokenizer "$models/tokenizer.model" >"$log" 2>&1 &
outpid=$!

for _ in $(seq 1 180); do
  kill -0 "$outpid" 2>/dev/null || { echo "FAIL: server exited"; cat "$log"; exit 1; }
  curl -sf -m 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1 && break
  sleep 0.5
done

python3 - "$port" <<'PY' || { cat "$log"; exit 1; }
import json, sys, urllib.error, urllib.request
body = json.dumps({"input": "hi", "voice": 'no"such.wav'}).encode()
req = urllib.request.Request(f"http://127.0.0.1:{sys.argv[1]}/v1/audio/speech", data=body,
                             headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(req, timeout=30)
    sys.exit("FAIL: a missing voice was answered with success")
except urllib.error.HTTPError as exc:
    raw = exc.read().decode(errors="replace")
try:
    message = json.loads(raw)["error"]["message"]
except (ValueError, KeyError, TypeError) as exc:
    sys.exit(f"FAIL: error body is not the JSON envelope ({exc}): {raw}")
if 'no"such.wav' not in message:
    sys.exit(f"FAIL: error message lost the voice name: {message}")
print("OK: missing-voice error is valid JSON:", message)
PY
