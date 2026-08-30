# Local TTS — Agent Guide

Faster-than-realtime voice cloning and TTS on CPU: one C++ file around stock
ONNX Runtime, plus a WASM build for the browser. This is **Tristan's project**
(private repo `tristan-mcinnis/local-tts`), forked from
`pkalogiros/pocket-tts-raven` (kept as the `upstream` remote for pulling
improvements). It is the TTS organ of the local-models layer: weights live in
the shared store at `~/Models/pocket-tts/` (registered in
`~/Models/models.json`), and the engine runs as an always-on localhost service
via launchd agent `com.tristan.local-tts`.

## Orientation

- `README.md` — full usage, CLI flags, C API, HTTP server. Read it first.
- `docs/RUNBOOK.md` — build/run/verify procedures for this fork.
- `docs/OPTIMIZATION_NOTES.md` — the full optimization history and how models
  are regenerated.
- `models/README.md`, `webdemo/models/README.md`, `webdemo/README.md` — what
  lands where, engines, WASM worker protocol, embedding the module.

## Layout

| path | what it is |
|---|---|
| `src/pocket_tts.cpp` | the whole runtime: CLI, HTTP server, streaming, caching, FFI |
| `src/ptt_custom_ops.cpp` | custom ORT ops (attention, fused conv) |
| `include/pocket_tts.h` | public C API (FFI), every function documented |
| `tools/make_*.py` | offline ONNX graph rewrites (delta-KV, dedup, merged flow, custom-op injection) |
| `tools/prepare_models.sh` | one-time model download (hash-verified) + rewrite pipeline |
| `tools/verify_model_equivalence.py` | lockstep checker for staged graph changes |
| `models/` | symlink to `~/Models/pocket-tts/` — the prepared ONNX bundle + tokenizer (generated, not hand-edited) |
| `voices/` | voice samples; `voices/.cache/` holds `.emb` and `.kv` caches |
| `webdemo/` | WASM browser demo (vendored build in `webdemo/vendor/ptt/`) |

## Build and run

```bash
cmake -B .build -DCMAKE_BUILD_TYPE=Release && cmake --build .build -j
./tools/prepare_models.sh          # one-time; --web for the webdemo model set
./pocket-tts "Hello." example.wav out.wav
```

- Requires CMake 3.28+, C++17, and `uv` for the model scripts.
- Build outputs land in the repo root (`pocket-tts`, `libptt_custom_ops.dylib`).
- Smoke test: `docs/RUNBOOK.md` has the verify procedures, including
  `tools/verify_model_equivalence.py` for model changes.

## Invariants (do not break)

1. **No ONNX Runtime fork or patches.** Speed comes from offline graph
   rewrites (`tools/`) plus a fast driver around stock ORT. ORT upgrades must
   stay a version bump. The WASM build uses unmodified ORT sources with
   official flags only.
2. **Models are generated artifacts.** Never hand-edit files in `models/` or
   `webdemo/models/`; change the rewrite scripts in `tools/` and rerun
   `prepare_models.sh`. Every downloaded file is sha256-pinned in the script.
3. **Math-preserving rewrites only.** Delta-KV steps run ORT equivalence
   checks; staged changes go through `verify_model_equivalence.py`.
4. **Graph rewrites and runtime are separate layers.** A new op goes through
   ORT's public custom-op API (`src/ptt_custom_ops.cpp`), not into ORT itself.
5. Audio is mono float32 at 24 kHz end to end (CLI, FFI, HTTP, WASM).

## Platform notes

- Attention custom ops are portable (Accelerate on Apple, WASM-SIMD in
  browser, NEON/SSE elsewhere). Conv custom ops are Apple-only; other
  platforms fall back to the plain decoder automatically.
- MSVC is unsupported for custom ops (no vector extensions); use clang-cl on
  Windows. `-DPTT_FORCE_PORTABLE` forces the portable backend for A/B tests.
- `-DBUILD_SHARED_LIB=ON` builds the FFI library; `-DPTT_NATIVE_OPT=ON` adds
  native CPU tuning.

## Responsible use (hard rule)

This runtime clones voices. Only use voices you own or have explicit, lawful
consent to use. Never use generated speech for impersonation, deception,
fraud, harassment, or to present synthetic audio as a genuine recording.
Upstream Pocket TTS model access carries prohibited-use terms; do not bypass
them.

## Repo hygiene

- Own project, private repo. `origin` = tristan-mcinnis/local-tts (push
  freely), `upstream` = pkalogiros/pocket-tts-raven (read-only; pull
  improvements with `git fetch upstream`). Forked 2026-08-30, MIT, attribution
  kept in README + THIRD_PARTY_NOTICES.
- Large binaries (`libonnxruntime*.dylib`, `pocket-tts`, models, voices
  cache) are gitignored or expected local artifacts; do not commit them.
- Model weights are shared-layer state, not repo state: regenerate with
  `tools/prepare_models.sh` (lands in `~/Models/pocket-tts/` via the `models/`
  symlink), never commit them.
- License: MIT (`LICENSE`), third-party attribution in
  `THIRD_PARTY_NOTICES.md` (notably lame.js, LGPL).
