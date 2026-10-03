# Personal setup (the author's Mac)

This page records how Local TTS runs on the author's machine, as part of the
House local-model layer. None of it is needed to build or use Local TTS; the
README covers that. Paths are written from `~`.

## Names

- Repository: `local-tts` (GitHub `tristan-mcinnis/local-tts`).
- Engine binary: `pocket-tts` (upstream's name), installed at
  `~/.local/libexec/local-tts/`.
- launchd job: `com.tristan.local-tts`, on `127.0.0.1:8081`. The template is
  `launchd/com.tristan.local-tts.plist.in`; `scripts/install-runtime.sh`
  renders it into `~/Library/LaunchAgents/`.
- Registry id: `pocket-tts` (alias `tts`) in `~/Models/models.json`, the
  shared registry that [Local Models](https://github.com/tristan-mcinnis/local-models)
  reads.
- Thin client on PATH: `~/.local/bin/local-tts`, a symlink to
  `cli/local-tts` in the checkout.

## Layout

- Weights: `~/Models/pocket-tts/`. In the checkout, `models/` is a
  machine-local symlink there (gitignored), so `tools/prepare_models.sh`
  writes straight into the shared store and the installer never copies the
  model set.
- Voices: the service reads `~/Models/pocket-tts/voices/` (caches in its
  `.cache/`). The checkout's `voices/` is the source the installer copies
  there; only `example.wav` is tracked, personal samples stay ignored.
- Logs: `~/Library/Logs/local-tts.log` and `local-tts.err.log`.

## Why the service runs a copy

launchd jobs get no Files-and-Folders consent dialog. A binary or dylib read
from `~/Documents` blocks the process silently at start (seen 2026-09-03,
after the repo moved under `~/Documents`). So the job runs the copy in
`~/.local/libexec/local-tts/` and reads voices from `~/Models`. Rerun
`scripts/install-runtime.sh` after every rebuild or new voice; a plain
`launchctl kickstart -k gui/$(id -u)/com.tristan.local-tts` reruns the old
copy.

## Consumers

- Quick Launch's Read Aloud calls `POST /v1/audio/speech` on port 8081 with
  its own voice choice (`vctk-p225.wav`, an ignored local sample).
- `cli/local-tts` now defaults to the shipped `example.wav`. To keep the old
  default voice in the shell, set `export LOCAL_TTS_VOICE=vctk-p225`.

## Reinstall note (2026-10-03)

The `pocket-tts` executable now loads `libonnxruntime` from beside itself
(`@executable_path`). Copies installed before that change still point at the
checkout's `.build/_deps/onnxruntime-src/lib`, so the live service depends on
`.build` until it is reinstalled. Rebuild and run
`scripts/install-runtime.sh` once to pick up the fix; afterwards
`otool -l ~/.local/libexec/local-tts/pocket-tts | grep -A2 LC_RPATH` shows
`@executable_path`.
