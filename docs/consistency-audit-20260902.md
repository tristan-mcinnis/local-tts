# Consistency audit, 2026-09-02

Goal: one way of doing things across Tristan's local Mac tools. Scope: this
repo only, behaviour unchanged, launchd service untouched (label and port kept).

## Found

- Contract: `AGENTS.md` existed with no `CLAUDE.md`, the reverse of every
  sibling repo. Content was accurate except a dangling `models/README.md` ref.
- Port and paths lived in two places: the launchd plist (8081, repo and
  weights paths) and `cli/local-tts` (8081, hardcoded `$HOME/Documents/code/...`
  voices path). The C++ binary defaults to 8080, docs used 8080 in examples.
- Duplicate runtime: `generate.py` + `pocket_tts_onnx.py` were an unreferenced
  Python inference path from upstream using a different bundle layout
  (`onnx/<language>/`) and `huggingface_hub`. Nothing in this repo imports them.
  `export_onnx.py` stays; `tools/prepare_models.sh` names it as the developer path.
- Binaries: `libonnxruntime*.dylib`, `libptt_custom_ops.dylib`, `pocket-tts`
  are build outputs sitting in the repo root, already gitignored and never in
  history (`git ls-files` shows only `voices/example.wav` as binary). `models/`
  is an ignored symlink to `~/Models/pocket-tts`. Nothing to purge.
- Registry: `~/Models/models.json` already registers `pocket-tts` (alias `tts`).
  `local-models` docs list TTS as a future backend; no change needed here.
- Logging: one approach (stderr/stdout, captured by launchd to
  `~/Library/Logs/local-tts*.log`). Fine.
- Docs: README and RUNBOOK still said "PocketTTS.cpp" in places and RUNBOOK
  had an upstream-only "alongside the game" note. `THIRD_PARTY_NOTICES.md` is
  current (Alba sample, lame.js LGPL, ORT, dr_libs, SentencePiece, fonts).
- `.gitignore` had three redundant `models/*` rules under a `/models` rule.
- Tests: `webdemo/npm test` runs three files; only the tokenizer test runs
  without generated fixtures (`xfer_states.bin`, `ref_*.f32` from the Python
  generators, which need the model bundle and ORT in Python). No native tests;
  the RUNBOOK smoke test is the check.
- Secrets/personal data: none tracked. Personal voice samples and caches are
  local-only (ignored).

## Fixed

- `CLAUDE.md` is the canonical contract (75 lines); `AGENTS.md` -> symlink.
- `cli/local-tts` resolves the voices dir from its own real path, so the
  `~/.local/bin` symlink survives a repo move; header names the plist as the
  source of truth for port and paths.
- `launchd/install.sh` installs or refreshes the agent from the checked-in
  plist (not run in this session; the live agent already matches the plist).
- Removed `generate.py` and `pocket_tts_onnx.py`.
- README: service section (8081, cli, restart command), product name in the
  drop-in sentence, dead `models/README.md` reference removed. RUNBOOK: title,
  service section pointing at the plist, upstream-only wording dropped.
- `.gitignore` de-duplicated.

## Verified

- `cmake` + Ninja build to a scratch `PTT_OUTPUT_DIR` (root binary untouched):
  exit 0, two upstream compiler warnings.
- Smoke: scratch binary generated speech from `example.wav` at temperature 0.
- `local-tts --health` returns ok; `local-tts "..."` produced a WAV via the
  running 8081 service; `--list` shows voices.
- `webdemo` tokenizer test: 15/15 vectors match.

## Left

- Deleting the two Python files diverges from upstream; a future
  `git merge upstream/main` will show modify/delete on them if upstream edits
  them. Resolve by keeping them deleted.

## Closed later the same day

- `webdemo/test/run.mjs` is now `npm test`: tokenizer always runs; xfer and
  engine skip with a printed reason when the web model set or the generated
  fixtures are missing. `npm run fixtures` (`test/gen_fixtures.sh`) regenerates
  the fixtures deterministically at temperature 0; it needs the web model
  weights, so it is not run on a clean checkout. Generated fixture binaries
  are gitignored. Result: 1 passed, 2 skipped, 0 failed.
- Native `ctest` target `smoke` (`tests/smoke.sh`): renders one sentence from
  `voices/example.wav` and checks the RIFF/WAVE header; exit 77 = skipped when
  `models/` has no bundle. Verified on a scratch `PTT_OUTPUT_DIR` build:
  passed, 238124-byte WAV; skip path verified against an empty dir.
