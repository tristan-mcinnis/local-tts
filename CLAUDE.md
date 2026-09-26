# Local TTS — Agent Guide

Canonical project contract. `AGENTS.md` is a symlink to this file.

## Goal

On-device voice cloning and text-to-speech, faster than realtime on CPU: one
C++ runtime (`src/pocket_tts.cpp`) around stock ONNX Runtime, plus a WASM build
for the browser. Tristan's private fork of `pkalogiros/pocket-tts-raven` (MIT)
and the TTS organ of the local-models layer. Nothing is uploaded, ever.

## Service on this Mac (source of truth: `launchd/com.tristan.local-tts.plist`)

- launchd label `com.tristan.local-tts`, `127.0.0.1:8081`, OpenAI-compatible
  `POST /v1/audio/speech`, streaming `POST /tts`, `GET /health`.
- Weights: `~/Models/pocket-tts/` (registered in `~/Models/models.json` as
  `pocket-tts`, alias `tts`). `models/` is a machine-local symlink there.
- Voices: the service reads `~/Models/pocket-tts/voices/` (caches in its
  `.cache/`). The repo's `voices/` is the source `scripts/install-runtime.sh`
  copies there (only `example.wav` is tracked).
- Logs: `~/Library/Logs/local-tts.log` and `local-tts.err.log`.
- Thin client `cli/local-tts` (on PATH via `~/.local/bin`); it reads the same
  port and voices dir, overridable with `LOCAL_TTS_URL` / `LOCAL_TTS_VOICES_DIR`.
- Install or refresh the agent with `scripts/install-runtime.sh`
  (`launchd/install.sh` calls it). The binary default port is 8080; the plist
  sets 8081. Change the port or paths in the plist only.

## Build, run, test

```bash
cmake -B .build -DCMAKE_BUILD_TYPE=Release && cmake --build .build -j
./tools/prepare_models.sh          # one-time; --web for the webdemo model set
./pocket-tts "Hello." example.wav out.wav
local-tts --health                 # service check
ctest --test-dir .build          # native smoke render; skipped if weights missing
(cd webdemo && npm ci && npm test) # tokenizer always; xfer/engine skip until `npm run fixtures`
```

- Needs CMake 3.28+, C++17, `uv` for model scripts, Node for webdemo tests.
- Build outputs land in the repo root (`pocket-tts`, `libptt_custom_ops.dylib`,
  `libonnxruntime*.dylib`), all gitignored. `-DPTT_OUTPUT_DIR=<dir>` redirects
  them. The service never runs the root binary (see Runtime layout).
- After a rebuild the service keeps running the old copy in
  `~/.local/libexec/local-tts/` until `scripts/install-runtime.sh` copies the
  new one and reloads the job; `launchctl kickstart -k` alone reruns the old copy.
- ctest runs four scripts, each exit 77 = skipped without weights:
  `tests/smoke.sh` (one render from `voices/example.wav`, WAV header check),
  `tests/smoke_bind.sh` (listener is 127.0.0.1 only), `tests/smoke_errors.sh`
  (error bodies stay valid JSON) and `tests/smoke_stall.sh` (a `/tts` client
  that stops reading is dropped after `--send-timeout`, default 30 s, so the
  synthesis lock is freed). `webdemo/test/run.mjs`
  runs the three Node tests and skips the two that need the web model set
  plus fixtures from `webdemo/test/gen_fixtures.sh` (deterministic, temp 0).
- `docs/RUNBOOK.md` has smoke tests, benchmarking rules, and model verification
  (`tools/verify_model_equivalence.py`). `docs/OPTIMIZATION_NOTES.md` has the
  optimization history.

## Layout

- `src/pocket_tts.cpp` runtime: CLI, HTTP server, streaming, caching, FFI.
- `src/ptt_custom_ops.cpp` custom ORT ops. `include/pocket_tts.h` public C API.
- `tools/make_*.py` offline ONNX graph rewrites; `tools/prepare_models.sh`
  downloads (sha256-pinned) and rewrites. `export_onnx.py` is the developer
  path that re-exports base models from PyTorch weights.
- `webdemo/` WASM browser demo (vendored build in `webdemo/vendor/ptt/`).

## Invariants (do not break)

1. No ONNX Runtime fork or patches; speed comes from offline graph rewrites
   plus a fast driver. ORT upgrades stay a version bump.
2. Models are generated artifacts: never hand-edit `models/` or
   `webdemo/models/`; change `tools/` and rerun `prepare_models.sh`.
3. Math-preserving rewrites only, checked with `verify_model_equivalence.py`.
4. New ops go through ORT's public custom-op API, never into ORT.
5. Audio is mono float32 at 24 kHz end to end.
6. Audio never leaves the machine. No telemetry.

## Boundaries and hygiene

- Never commit weights, dylibs, the binary, voice caches, or personal voice
  samples. Model weights are shared-layer state, not repo state.
- No secrets in the repo; there are none today and none are needed.
- `origin` = tristan-mcinnis/local-tts (push to main on request).
  `upstream` = pkalogiros/pocket-tts-raven, read-only, for pulling improvements.
- Attribution stays: README "Forked from" line and Acknowledgments, plus
  `THIRD_PARTY_NOTICES.md` (lame.js is LGPL). License MIT.
- Responsible use: only clone voices you own or have consent to use. Never
  present synthetic audio as a genuine recording. Upstream model terms apply.

## Runtime layout (2026-09-03)

The launchd job never reads from this repo. `scripts/install-runtime.sh`
copies `pocket-tts` and its dylibs to `~/.local/libexec/local-tts/`, the
voices to `~/Models/pocket-tts/voices/`, and the plist from `launchd/` into
`~/Library/LaunchAgents/`. Reason: a launchd process gets no Files-and-Folders
consent dialog, so a binary or dylib under `~/Documents` blocks silently at
start. Re-run the script after rebuilding `pocket-tts` or adding a voice.
