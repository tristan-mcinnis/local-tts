#!/usr/bin/env bash
# Native bind regression test for ctest: start a `--server` and verify the
# listener is bound to 127.0.0.1 (loopback), owned by the spawned pid, and serves
# /health. This FAILS (not merely warns) if someone reverts the bind to INADDR_ANY
# (all interfaces) — the lsof address column would be `*:port` instead of
# `127.0.0.1:port`. Exit 77 (skipped) when the model bundle is absent; weights are
# never in git.
#   tests/smoke_bind.sh <pocket-tts binary> <models dir> <voices dir>
set -u
bin="${1:?binary}"; models="${2:?models dir}"; voices="${3:?voices dir}"

if [ ! -f "$models/tokenizer.model" ] || ! ls "$models"/flow_lm_main*.onnx >/dev/null 2>&1; then
  echo "SKIP: no model bundle at $models (run tools/prepare_models.sh)"
  exit 77
fi
[ -x "$bin" ] || { echo "FAIL: binary not found at $bin"; exit 1; }
[ -f "$voices/example.wav" ] || { echo "FAIL: $voices/example.wav missing"; exit 1; }
command -v lsof >/dev/null 2>&1 || { echo "FAIL: lsof not on PATH (required to verify loopback bind)"; exit 1; }

# Fresh temp voices dir so the server writes its .emb/.kv caches here, never to
# the production voices dir (or the repo voices dir).
voices_tmp="$(mktemp -dt ptt-bind-voices.XXXXXX)"
cp "$voices/example.wav" "$voices_tmp/example.wav"

# Pick a free loopback port; lsof ownership check below guards the tiny bind race.
port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"

log="$(mktemp -t ptt-bind.XXXXXX).log"
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

# Start the server; loopback-only is the default after the bind fix.
"$bin" --server --port "$port" --models-dir "$models" \
  --voices-dir "$voices_tmp" --tokenizer "$models/tokenizer.model" >"$log" 2>&1 &
outpid=$!

# Wait for a LISTEN on the port, then require: the owner is our pid AND the
# address column is 127.0.0.1 (not `*` = all interfaces).
for _ in $(seq 1 180); do
  if ! kill -0 "$outpid" 2>/dev/null; then
    echo "FAIL: server exited before listening (rc-check below)"; cat "$log"; exit 1
  fi
  # Parse `lsof -nP -Fnp` structured output: `p<pid>` record start, `f<fd>`
  # (ignored), `n<host>:<port>` name. The name has no TCP/(LISTEN) decoration,
  # so we split the `n` value on the last `:`.
  lsof_out="$(lsof -nP -Fnp -iTCP:"$port" -sTCP:LISTEN 2>/dev/null)"
  if [ -n "$lsof_out" ]; then
    pid_field="$(printf '%s\n' "$lsof_out" | grep '^p' | head -n 1 | cut -c2-)"
    name_field="$(printf '%s\n' "$lsof_out" | grep '^n' | head -n 1 | cut -c2-)"
    if [ -n "$pid_field" ] && [ -n "$name_field" ]; then
      listen_host="${name_field%:*}"
      listen_port="${name_field##*:}"
      if [ "$pid_field" != "$outpid" ]; then
        echo "FAIL: pid $pid_field owns port $port, not the spawned $outpid (foreign listener -> bind race)"
        exit 1
      fi
      if [ "$listen_host" != "127.0.0.1" ] || [ "$listen_port" != "$port" ]; then
        echo "FAIL: listener not loopback-only. NAME='$name_field' (expected '127.0.0.1:$port'). Revert of bind fix?"
        exit 1
      fi
      break
    fi
  fi
  sleep 0.5
done

if [ -z "${name_field:-}" ]; then
  echo "FAIL: no LISTEN on 127.0.0.1:$port within 90s"; cat "$log"; exit 1
fi

# The emitted server log must reflect the actual loopback address.
if ! grep -q "127\.0\.0\.1:$port" "$log"; then
  echo "FAIL: server log does not reflect 127.0.0.1:$port"; cat "$log"; exit 1
fi

# /health must answer 200 on loopback.
health="$(python3 -c 'import urllib.request,sys; print(urllib.request.urlopen("http://127.0.0.1:"+sys.argv[1]+"/health", timeout=5).status)' "$port" 2>/dev/null)"
if [ "$health" != "200" ]; then
  echo "FAIL: /health did not return 200 (got '$health')"; cat "$log"; exit 1
fi

echo "OK: listener bound to 127.0.0.1:$port, owned by pid $outpid, /health=200"
exit 0
