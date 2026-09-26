#!/usr/bin/env bash
# Stalled-reader regression test for ctest. Synthesis is serialized by one
# mutex, and /tts streams while it holds that mutex. A client that opens a long
# /tts stream and then stops reading used to block send() forever, which
# wedged every later request while /health still answered. The server now sets
# a send timeout (--send-timeout, default 30 s; 2 s here) and drops a reader
# that makes no progress. Pass = a short /v1/audio/speech call made behind the
# stalled stream succeeds within 60 s. Exit 77 (skipped) when the model bundle
# is absent; weights are never in git.
#   tests/smoke_stall.sh <pocket-tts binary> <models dir> <voices dir>
set -u
bin="${1:?binary}"; models="${2:?models dir}"; voices="${3:?voices dir}"

if [ ! -f "$models/tokenizer.model" ] || ! ls "$models"/flow_lm_main*.onnx >/dev/null 2>&1; then
  echo "SKIP: no model bundle at $models (run tools/prepare_models.sh)"
  exit 77
fi
[ -x "$bin" ] || { echo "FAIL: binary not found at $bin"; exit 1; }

# A fresh voices dir, so nothing is cached into the real one.
voices_tmp="$(mktemp -dt ptt-stall-voices.XXXXXX)"
cp "$voices/example.wav" "$voices_tmp/example.wav"
port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
log="$(mktemp -t ptt-stall.XXXXXX).log"
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

"$bin" --server --port "$port" --send-timeout 2 --models-dir "$models" \
  --voices-dir "$voices_tmp" --tokenizer "$models/tokenizer.model" >"$log" 2>&1 &
outpid=$!

for _ in $(seq 1 180); do
  kill -0 "$outpid" 2>/dev/null || { echo "FAIL: server exited"; cat "$log"; exit 1; }
  curl -sf -m 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1 && break
  sleep 0.5
done

python3 - "$port" <<'PY' || { tail -c 1500 "$log"; exit 1; }
import json, socket, sys, time, urllib.request
port = int(sys.argv[1])

# Open a long /tts stream with a tiny receive window, read the first bytes,
# then stop reading. Minutes of float32 audio overflow every socket buffer.
text = " ".join(f"This is sentence number {i} of a long passage that nobody reads." for i in range(200))
body = json.dumps({"text": text, "voice": "example.wav"}).encode()
stalled = socket.socket()
stalled.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
stalled.connect(("127.0.0.1", port))
stalled.sendall(
    b"POST /tts HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
    + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
stalled.settimeout(60)
if not stalled.recv(64).startswith(b"HTTP/1.1 200"):
    sys.exit("FAIL: /tts did not start a stream")
time.sleep(1)  # the stream is now blocked on this socket

start = time.monotonic()
req = urllib.request.Request(
    f"http://127.0.0.1:{port}/v1/audio/speech",
    data=json.dumps({"input": "Hello.", "voice": "example.wav"}).encode(),
    headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        wav = resp.read()
except Exception as exc:
    sys.exit(f"FAIL: a request behind a stalled /tts reader did not finish ({exc!r})")
finally:
    stalled.close()
if wav[:4] != b"RIFF":
    sys.exit("FAIL: the request behind the stalled reader returned no WAV")
print(f"OK: request behind a stalled /tts reader answered in {time.monotonic() - start:.1f}s")
PY
