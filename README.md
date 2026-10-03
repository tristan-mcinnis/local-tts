<p align="center">
  <img src="docs/icon.png" width="128" height="128" alt="Local TTS icon">
</p>

<h1 align="center">Local TTS</h1>

<p align="center"><strong>Fast on-device voice cloning and text-to-speech, on CPU and in the browser.</strong></p>

<p align="center">
  <img alt="Platform" src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20browser-1f2937">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-3b5bdb">
  <img alt="Free and open source" src="https://img.shields.io/badge/free-and%20open%20source-3b5bdb">
  <img alt="Runs locally" src="https://img.shields.io/badge/runs-locally-1f2937">
</p>

Local TTS turns text into speech on your own machine, in a voice cloned from
6 to 15 seconds of audio. It is one C++ runtime around stock ONNX Runtime for
[Kyutai's Pocket TTS](https://github.com/kyutai-labs/pocket-tts) model: a CLI,
a localhost HTTP server with an OpenAI-compatible endpoint, a C API, and the
same engine compiled to WebAssembly for the browser. It is for people who want
good speech without a cloud account, a GPU or a Python stack.

> **Free and open source.** Local TTS is free to use, change and share under the MIT License.
> No account, no subscription, no telemetry. Text and audio stay on your machine; the network is used only to fetch build dependencies and to download the model weights once.

Local TTS is a fork of [pkalogiros/pocket-tts-raven](https://github.com/pkalogiros/pocket-tts-raven)
by Pantelis Kalogiros, which builds on [VolgaGerm/PocketTTS.cpp](https://github.com/VolgaGerm/PocketTTS.cpp)
and on Kyutai Labs' Pocket TTS. The speed work is theirs; see [Credits](#credits).
Upstream demo: [pantel.is/projects/pocket-tts-raven](https://pantel.is/projects/pocket-tts-raven/).

![PocketTTS-RAVEN](webdemo/og-card.png)

## Features

|                       | speed          | first audio | notes |
|-----------------------|----------------|-------------|-------|
| **Native** (M4 Max)   | ~33x realtime  | ~30 ms      | no Python, no GPU, no frameworks |
| **Browser** (desktop) | ~14x realtime  | ~70 ms      | WASM + SIMD + threads |
| **Browser** (iPhone)  | ~3-4x realtime | <250 ms     | same build |

- **Voice cloning on-device.** The voice argument is any WAV or MP3 of one
  person speaking: a file, the microphone in the web demo, or a buffer over
  the C API. Nothing is uploaded.
- **Streaming.** Audio starts in tens of milliseconds and plays while the rest
  is generated.
- **Three ways in.** CLI, a localhost HTTP server (`/v1/audio/speech` is
  OpenAI-compatible, `/tts` streams raw PCM), and a documented C API for FFI.
- **Browser build.** The same runtime as WASM, with streaming playback, voice
  cloning from file or microphone, and WAV/MP3 export.
- **Stock runtime, fast models.** No ONNX Runtime fork. The speed comes from
  offline, math-preserving graph rewrites plus a fast driver (see
  [Design](#design-fast-models-stock-runtime)).
- **Caching.** Voice embeddings and KV state are cached on disk, so a voice
  you have used before starts in milliseconds.
- **Optional always-on service** on macOS (launchd), with a thin `local-tts`
  client.

## Requirements

- **To build:** CMake 3.28+ and a C++17 compiler. CMake fetches ONNX Runtime
  1.23.2 (prebuilt, sha256-pinned), SentencePiece v0.2.1 and dr_libs.
- **To get the model:** `curl`, Python 3 and [uv](https://docs.astral.sh/uv/).
  The first run of `tools/prepare_models.sh` downloads about 165 MB from
  Hugging Face.
- **OS:** this fork is built and tested on macOS on Apple Silicon. The CMake
  build also targets Linux (x86-64 and ARM64), as upstream does, but it is not
  tested here. The custom attention op speeds up every GCC/Clang target; the
  fused-conv custom ops are Apple-only. The launchd service is macOS only.
- **For the web demo tests:** Node.js.
- **Optional:** [Local Models](https://github.com/tristan-mcinnis/local-models)
  can list the weights in its shared `~/Models` registry (see
  [docs/personal-setup.md](docs/personal-setup.md)). Local TTS does not need it.

## Install

### Download

Get the latest `.tar.gz` and `SHA256SUMS` from the [Releases page](https://github.com/tristan-mcinnis/local-tts/releases/latest). It is for Apple Silicon Macs on macOS 13 or later. It holds the `pocket-tts` binary, ONNX Runtime, the `local-tts` client, an example voice and an installer. The model weights are not inside (CC BY 4.0); you download them once.

```bash
shasum -a 256 -c SHA256SUMS                        # check the download
tar -xzf local-tts-<version>-macos-arm64.tar.gz
xattr -dr com.apple.quarantine local-tts-<version>-macos-arm64
cd local-tts-<version>-macos-arm64
./tools/prepare_models.sh                          # one time, about 165 MB
./install.sh                                       # installs to ~/.local, or --prefix DIR
pocket-tts "Hello from Local TTS." example.wav hello.wav
```

Add `~/.local/bin` to your PATH if it is not there. The installer puts `pocket-tts` and `local-tts` in that folder.

Local TTS is not notarized. It is a free project, and it has no paid Apple Developer ID. So macOS may block the first run. Only use the files if you downloaded them from the Releases page of this repository. If macOS blocks a file, open System Settings, then Privacy & Security. Scroll down and click Open Anyway next to the blocked file. Confirm. Or run the `xattr` line above, which removes the quarantine flag. Each release is signed ad hoc. So macOS may ask again for permissions such as Accessibility or Microphone after an update. Grant them again when asked.

### Build from source

```bash
git clone https://github.com/tristan-mcinnis/local-tts.git
cd local-tts

# 1. build the native runtime (outputs land in the repo root)
cmake -B .build -DCMAKE_BUILD_TYPE=Release && cmake --build .build -j

# 2. get the model (one-time: downloads the Pocket TTS ONNX bundle,
#    ~165 MB, hash-verified, then applies the graph rewrites locally)
./tools/prepare_models.sh

# 3. speak with the bundled Alba sample voice
./pocket-tts "The road ahead is more dangerous than it looks." example.wav out.wav
```

The build copies `libonnxruntime*.dylib` (or `.so`) next to `pocket-tts`, and
the binary finds it there (`@executable_path` / `$ORIGIN`). To move the
runtime, copy the binary, the dylibs and `third_party_licenses/` together.

### Optional: run it as a service on macOS

`scripts/install-runtime.sh` installs an always-on launchd agent on
`127.0.0.1:8081`:

```bash
./scripts/install-runtime.sh
ln -s "$PWD/cli/local-tts" ~/.local/bin/local-tts   # optional thin client
local-tts --health
local-tts "Hello from the service." --play
```

It copies `pocket-tts`, its dylibs and the license files to
`~/.local/libexec/local-tts/`, copies `voices/` to `~/Models/pocket-tts/voices/`,
copies the model set to `~/Models/pocket-tts/` if it is not there yet, renders
`launchd/com.tristan.local-tts.plist.in` (its `@HOME@` placeholders become your
home directory) into `~/Library/LaunchAgents/`, and loads it. The job runs a
copy outside the repo because launchd may be refused access to `~/Documents`.
Logs go to `~/Library/Logs/local-tts.log` and `local-tts.err.log`.

Rerun the script after a rebuild or after adding a voice; `launchctl kickstart`
alone restarts the old copy. The label is `com.tristan.local-tts`; to use your
own, change `LABEL` in the script and rename the template to match. Change the
port or paths in the template.

## Usage

### Responsible use

This runtime can clone voices. Only use voices you own or have explicit,
lawful consent to use. Do not use generated speech for impersonation,
deception, fraud, harassment, privacy-invasive content, or to present
synthetic audio as a genuine recording. Kyutai's Pocket TTS model terms carry
the same prohibited uses, and they apply to you however you download the
weights.

### Clone a voice

Cloning is the same command as speaking: the voice argument is any WAV or MP3.

```bash
cp ~/my-recording.wav voices/me.wav      # 6-15 s of one person speaking
./pocket-tts "Now I speak with your voice." me.wav cloned.wav
```

The embedding is computed on first use and cached in `voices/.cache/`, so the
second run with the same voice starts in milliseconds. The bundled
`voices/example.wav` and the browser preset come from Kyutai's Alba MacKenna
`casual.wav` sample (CC BY 4.0).

### CLI

```bash
./pocket-tts [OPTIONS] TEXT VOICE [OUTPUT]

# pipe raw PCM to a player
./pocket-tts --stdout "Hello." me.wav | ffplay -f f32le -ar 24000 -nodisp -autoexit -

# fastest first audio
./pocket-tts --low-latency --trim-leading "Hello." me.wav out.wav

# let the model voice clause pauses (nicer for prose)
./pocket-tts --keep-commas "First, we dig. Then, we descend." me.wav out.wav
```

### HTTP server

```bash
./pocket-tts --server --port 8080
```

The server listens on `127.0.0.1` only. Endpoints:

- `POST /v1/audio/speech`: OpenAI-compatible TTS (JSON body: `{"input": "...", "voice": "..."}`)
- `POST /tts`: streaming TTS (JSON body: `{"text": "...", "voice": "..."}`)
- `GET /health`: health check

Any client that supports OpenAI's TTS API (SillyTavern, Open WebUI and others)
can use Local TTS by pointing its base URL at `http://localhost:8080`. The
`model` and `speed` fields are accepted but ignored. `response_format` can be
`wav` (default) or `pcm`.

```bash
curl -X POST http://localhost:8080/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model": "tts-1", "input": "Hello world!", "voice": "example.wav", "response_format": "wav"}' \
  --output speech.wav
```

`/tts` streams raw chunked PCM (`audio/pcm;rate=24000;encoding=float;bits=32`)
for low-latency use. A client that stops reading is dropped after
`--send-timeout` seconds (default 30), so one stuck client cannot hold the
synthesis lock.

### Thin client

`cli/local-tts` talks to a running server (default `http://127.0.0.1:8081`,
the service port):

```bash
local-tts "Read this aloud." --play       # writes a WAV to /tmp and plays it
echo "From stdin." | local-tts -o out.wav
local-tts --list                          # voices the service reads
local-tts --voice me "In my voice."
```

Overrides: `LOCAL_TTS_URL`, `LOCAL_TTS_VOICE` (default `example.wav`),
`LOCAL_TTS_VOICES_DIR` (default `~/Models/pocket-tts/voices`).

### C API (FFI)

Public header: [`include/pocket_tts.h`](include/pocket_tts.h), with every
function documented. Build the shared library with `-DBUILD_SHARED_LIB=ON`, or
compile `src/pocket_tts.cpp` with `PTT_SHARED_LIB`. Audio is mono float32 at
24 kHz throughout.

```c
void* tts = ptt_create("models", "voices", "models/tokenizer.model",
                       "int8", 0.7f, /*lsd_steps*/ 1, /*threads*/ 0);
void* s = ptt_stream_start(tts, "Hello there.", "me.wav");
float* chunk; int n;
while (ptt_stream_read(s, &chunk, &n) == 1) {   // blocks between chunks
    play(chunk, n);
    ptt_free_audio(chunk);
}
ptt_stream_end(s);
ptt_destroy(tts);
```

| group | functions |
|---|---|
| lifecycle | `ptt_create`, `ptt_create_ex` (decoder-only / deferred-encoder / encoder-only modes), `ptt_destroy`, `ptt_warmup` |
| streaming | `ptt_stream_start`, `_read` (blocking), `_poll` (non-blocking), `_stop` (graceful abort), `_end`, `ptt_free_audio` |
| tuning | `ptt_set_temperature`, `ptt_set_soften_commas`, `ptt_set_max_chunk`, `ptt_configure_pool` |
| voice | `ptt_encode_voice`, `ptt_load_encoder` (cloning is otherwise implicit via the voice argument) |
| split pipelines | `ptt_latents_start/poll/stop/end` (raw latent frames out), `ptt_decode`, `ptt_decoder_reset` |
| diagnostics | `ptt_set_profiling`, `ptt_print_profile`, `ptt_debug_bench` |

### The web demo

```bash
# one-time: install the web model set into webdemo/models/ (+ brotli precompress)
./tools/prepare_models.sh --web
python3 webdemo/serve.py
#  -> http://localhost:8093          (this machine)
#  -> https://<your-ip>:8094         (other devices; accept the cert once)
```

The same C++ runtime compiled to WASM (see `webdemo/wasm/`): streaming
playback while generating, voice cloning from file or microphone with a
trim-selectable preview, optional round-trip editing through
[AudioMass](https://audiomass.co), and WAV/MP3 export. With precompressed
assets, the first visit transfers roughly 65-67 MB for the selected engine's
runtime plus non-encoder models (cached by the browser); the first voice clone
fetches a further ~14 MB compressed Mimi encoder (~38 MB raw).
`webdemo/README.md` covers the engines, the worker message protocol,
**using the WASM module in your own page**, and how to rebuild it.

### Caching

The runtime keeps two disk caches under `voices/.cache/`:

- **Voice embeddings (`.emb`)**: the Mimi encoder output for each voice
  sample, so the same WAV is not re-encoded on every run.
- **KV state snapshots (`.kv`)**: the transformer's KV cache after voice
  conditioning. A cold start spends hundreds of milliseconds here; a cached
  `.kv` file restores in about 4 ms. For multi-sentence input the snapshot is
  also held in memory, so only the first sentence pays the disk load.

Caches are invalidated when the source WAV changes. Clear them with
`rm -rf voices/.cache/`, or disable them with `--no-cache`.

For low-latency speech with leading-blip protection, use `--low-latency
--trim-leading` and keep the default `--trim-sustain 15`. `--trim-sustain 0`
restores the legacy behavior and can emit an isolated leading burst on some
voices.

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `-h`, `--help` | | Show usage and all options |
| `--precision` | `int8` | Model precision (`int8` or `fp32`) |
| `--temperature` | `0.7` | Sampling temperature |
| `--lsd-steps` | `1` | Flow matching ODE solver steps |
| `--eos-threshold` | `-4.0` | EOS detection threshold (lower = later cutoff) |
| `--eos-extra` | `-1` | Extra frames after EOS (`-1` = auto from text length) |
| `--noise-clamp` | `0` | Clamp noise magnitude (`0` = disabled, matches upstream) |
| `--threads` | `0` | Total thread budget (`0` = half of available cores) |
| `--models-dir` | `models` | Path to ONNX model directory |
| `--voices-dir` | `voices` | Path to voice samples directory |
| `--tokenizer` | `models/tokenizer.model` | Path to SentencePiece tokenizer |
| `--low-latency` | | Use a shorter startup fade and skip the leading gate unless `--trim-leading` is set |
| `--trim-leading` | | Keep the startup leading gate in low-latency mode |
| `--trim-confirm` | `2` | 10 ms speech detector windows required before accepting speech |
| `--trim-sustain` | `15` | 10 ms windows observed before emitting onset; keep at 15 for blip protection |
| `--trim-collapse` | `3` | Consecutive quiet windows that discard a spurious leading burst |
| `--no-cache` | | Disable all disk caching (`.emb` and `.kv` files) |
| `--no-delta-kv` | | Disable the delta-KV `flow_lm_main` model when present |
| `--stdout` | | Output raw f32le PCM to stdout |
| `--verbose` | | Enable verbose output |
| `--profile` | | Print a per-operation timing report after generation |
| `--server` | | Start HTTP server mode |
| `--port` | `8080` | Server port |
| `--send-timeout` | `30` | Seconds a client socket may accept no bytes before the server drops it (0 = never) |

Run `./pocket-tts --help` for the complete list (thread overrides,
leading-trim gate tuning, chunk sizing, model-variant toggles).

## Privacy

- **What it reads:** the text you pass, the voice files you name (from
  `--voices-dir`), and the model files.
- **What it sends:** nothing. Synthesis runs on your CPU. The HTTP server binds
  `127.0.0.1` only. There is no telemetry and no update check.
- **What it stores:** voice embeddings and KV snapshots in `voices/.cache/`,
  and the output files you ask for. The server prints each request's text and
  voice name to stdout; under the launchd service that goes to
  `~/Library/Logs/local-tts.log`.
- **Network use:** CMake downloads its pinned dependencies at build time, and
  `tools/prepare_models.sh` downloads the model files from Hugging Face once.
- **Web demo:** synthesis and cloning run in the browser tab; voice audio does
  not leave the page. The page loads two fonts from Google Fonts.
  `webdemo/serve.py` listens on all interfaces (8093 HTTP, 8094 HTTPS) so other
  devices on your network can open it; stop it when you are done.

## Build from source

```bash
cmake -B .build -DCMAKE_BUILD_TYPE=Release && cmake --build .build -j
./tools/prepare_models.sh             # --web also installs the web model set
ctest --test-dir .build --output-on-failure
python3 -m unittest discover -s tests -p 'test_*.py'
(cd webdemo && npm ci && npm test)
```

- Build outputs (`pocket-tts`, `libonnxruntime*.dylib`,
  `libptt_custom_ops.dylib`, `third_party_licenses/`) land in the repo root and
  are gitignored. `-DPTT_OUTPUT_DIR=<dir>` puts them elsewhere.
- `-DPTT_NATIVE_OPT=ON` adds `-mcpu=native` and LTO for your own machine.
- `ctest` runs the publish scrub (`tests/scrub.sh`) and four smoke tests: one
  render, loopback-only bind, valid JSON error bodies, and the stalled-client
  drop. The smoke tests skip (exit 77) until the model set is in `models/`.
- The web demo's `npm test` always runs the tokenizer test; the state-transfer
  and engine tests skip until you run `./tools/prepare_models.sh --web` and
  `npm run fixtures`.
- [docs/RUNBOOK.md](docs/RUNBOOK.md) has benchmarking rules and model
  verification; [docs/OPTIMIZATION_NOTES.md](docs/OPTIMIZATION_NOTES.md) has
  the optimization history.

### Design: fast models, stock runtime

The speed comes from two layers, with **no ONNX Runtime fork and no patches**:

1. **Offline graph rewrites** (`tools/`): the exported ONNX models are
   rewritten into faster, math-preserving graphs: delta-KV caching,
   cross-layer dedup, merged flow, custom-op injection. The delta-KV rewrites
   run built-in ORT equivalence checks; `tools/verify_model_equivalence.py`
   checks staged graph changes against their predecessor.
2. **A fast driver around stock ORT** (`src/pocket_tts.cpp`): persistent
   cache buffers, pipelined generate/decode threads, KV snapshots, and custom
   operators registered through ONNX Runtime's public custom-op API. The WASM
   build compiles unmodified ORT sources with official build flags only.

Upgrading ONNX Runtime is a version bump, not a rebase.

### Models

`./tools/prepare_models.sh` downloads the Pocket TTS ONNX bundle from Hugging
Face (a third-party ONNX export of Kyutai's weights; every file is checked
against a sha256 pinned in the script) and applies the graph-rewrite pipeline
locally. The pipeline is deterministic. `BUNDLE_URL=...` points it at another
source. `webdemo/models/README.md` lists the web set. `export_onnx.py` is the
developer path that re-exports the base models from the PyTorch weights.
Weights are never committed.

### Rebuilding the WASM engine (optional)

The compiled engine ships in `webdemo/vendor/ptt/`, so you only need this if
you change `src/pocket_tts.cpp` and want the change in the browser:

```bash
# once (~1 h): ONNX Runtime v1.27.0 as a WASM static library, beside the repo
git clone --recursive --branch v1.27.0 \
    https://github.com/microsoft/onnxruntime ../ort-wasm
(cd ../ort-wasm && ./build.sh --config Release --build_wasm_static_lib \
    --enable_wasm_threads --enable_wasm_simd \
    --disable_wasm_exception_catching --enable_wasm_api_exception_catching \
    --skip_tests --parallel)

# then rebuild the module (installs into webdemo/vendor/ptt/)
./webdemo/wasm/build.sh
```

The second step reuses the emsdk that ORT's build installs.
`ORT_WASM_ROOT=/path/to/ort-wasm` overrides the default location.

## Part of House

Local TTS is one of a small family of free, local-first Mac tools that share one design system.

| App | What it does |
|---|---|
| [Quick Launch](https://github.com/tristan-mcinnis/quick-launch) | Keyboard-first launcher and instant AI overlay. |
| [Local Dictation](https://github.com/tristan-mcinnis/local-dictation) | Hold a key, talk, and on-device text lands at your cursor. |
| **[Local TTS](https://github.com/tristan-mcinnis/local-tts)** | Fast on-device voice cloning and text-to-speech. |
| [Local Models](https://github.com/tristan-mcinnis/local-models) | One local daemon that serves a fleet of small models to every app. |
| [Usage](https://github.com/tristan-mcinnis/usage-menubar) | One menu-bar gauge for every AI subscription and API key. |

## Credits

Local TTS stands on other people's work:

- **Pantelis Kalogiros**, [pocket-tts-raven](https://github.com/pkalogiros/pocket-tts-raven)
  (MIT): the optimized runtime, graph rewrites, web demo and WASM build this
  repository is forked from. He also wrote [AudioMass](https://audiomass.co),
  which the web demo can use.
- **Kyutai Labs**, [Pocket TTS](https://github.com/kyutai-labs/pocket-tts): the
  model, the original Python implementation (MIT) and the weights
  ([CC BY 4.0](https://huggingface.co/kyutai/pocket-tts)), plus the Alba
  MacKenna sample voice (CC BY 4.0).
- **VolgaGerm**, [PocketTTS.cpp](https://github.com/VolgaGerm/PocketTTS.cpp)
  (MIT): the single-file C++ runtime upstream builds on.
- **KevinAHM**, [pocket-tts-onnx](https://huggingface.co/KevinAHM/pocket-tts-onnx)
  (CC BY 4.0): the ONNX export `prepare_models.sh` downloads.
  **Verylicious**, [pocket-tts-ungated](https://huggingface.co/Verylicious/pocket-tts-ungated)
  (CC BY 4.0): the weights `export_onnx.py` downloads.
- [ONNX Runtime](https://github.com/microsoft/onnxruntime) (MIT),
  [SentencePiece](https://github.com/google/sentencepiece) (Apache-2.0),
  [dr_libs](https://github.com/mackron/dr_libs) (public domain or MIT-0) and
  [lame.js](https://github.com/zhuker/lamejs) (LGPL, for MP3 export in the
  browser).

Every component, its license and the full license texts are in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

MIT. See [LICENSE](LICENSE). Third-party components keep their own licenses,
listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md); note that lame.js
in the web demo is LGPL and the model weights are CC BY 4.0.
