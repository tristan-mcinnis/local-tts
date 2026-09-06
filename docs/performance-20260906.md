# Local TTS thread-only performance benchmark (2026-09-06)

Objective: establish a **repeatable, thread-only** comparison on the **installed**
`pocket-tts` binary. The only thing that varies across runs is the ONNX Runtime
thread count; everything else is pinned. Results are reported as **median + range**
over n≥7 measured samples (never p95 at n=7), so the numbers survive the
thermal/scheduling noise on a busy Mac.

This is a measurement harness. It does **not** build the binary, touch runtime
config, or install anything — it only shells out to an already-installed binary.

## Runner

```bash
python3 tools/benchmark_threads.py \
  --output-dir /tmp/house-performance-20260906 \
  --samples 7 --warmup 1 --seed 0
```

Pure stdlib (no numpy/onnx/pytest). Dry-run first to see the exact plan and
commands without executing:

```bash
python3 tools/benchmark_threads.py --dry-run --output-dir /tmp/house-performance-20260906
```

The installed binary is `~/.local/libexec/local-tts/pocket-tts`, weights are
`~/Models/pocket-tts`, tokenizer is `~/Models/pocket-tts/tokenizer.model`, and
the stock voice is `voices/example.wav`. All of these are overridable
(`--binary`, `--models-dir`, `--voices-dir`, `--voice`, `--tokenizer`).

The phrase is the built-in fixed synthetic screening phrase; override it on a
second run to validate the winner on a distinct (typically longer) phrase:

```bash
# screening (one fixed synthetic phrase)
python3 tools/benchmark_threads.py --output-dir /tmp/house-performance-20260906 \
  --samples 7 --warmup 1 --seed 0
# winner validation on a separate, longer synthetic phrase (the run used)
python3 tools/benchmark_threads.py --output-dir /tmp/house-performance-20260906 \
  --samples 7 --warmup 1 --seed 1 \
  --text "The morning meeting begins at nine. Please bring the research notes and leave enough time for questions. This longer local test checks that clear speech remains consistent across several sentences."
```

## Pinned flags (same for every config)

```text
--temperature 0        deterministic latent sequence (the one deliberate deviation
                       from production, which uses 0.7; comparison-only)
--profile              emit RTFx + First chunk latency + per-session report
```

Everything else stays at the **binary's defaults** so both sides (running service
and benchmark) are on identical defaults. In particular **voice caches are
preserved** — no `--no-cache` — exactly as the real service uses them (the pilot
run already warmed the `example.wav` cache), and there are **no** `--low-latency`/
`--trim-*` overrides. Model precision stays the default `int8`; no `--no-*`
opt-out flags are passed, so the default optimized fast path is exercised.
Options that only fix model/voice paths (`--models-dir`, `--tokenizer`,
`--voices-dir`) are also constant.

## What varies: thread configs

| config   | threads-ar | threads-dec | threads-full | meaning |
|----------|-----------|-------------|--------------|---------|
| `auto`   | —         | —           | —            | no thread flags → binary's own derived budget (`--threads 0` = half cores, auto-balanced) |
| `2/2/4`  | 2         | 2           | 4            | flow_lm_main/flow = 2, Mimi decoder = 2, encoder/text-conditioner = 4 |
| `3/2/2`  | 3         | 2           | 2            | the runbook sweet spot: fewer decoder (Accelerate-bound), more AR |
| `2/1/2`  | 2         | 1           | 2            | minimal decoder thread |

(threads-ar → `flow_lm_main`/`flow`; threads-dec → `mimi_decoder`;
threads-full → encoder/text-conditioner, per `src/pocket_tts.cpp`.)

## Methodology

- **One fixed synthetic phrase per run.** `--text` overrides it (default is the
  built-in screening phrase above). Every config and every sample uses that same
  phrase, so runs are compared on identical input. **Phrase timings are never
  aggregated across different phrases** — that would produce a single misleading
  figure. Instead: screen all configs on one phrase, then validate the winner on
  a distinct longer phrase in a separate run (see [Phrase
  stratification](#phrase-stratification)).
- **Defaults preserved, caches kept.** Only `--temperature 0` and `--profile` are
  passed on top of the binary's defaults; voice `.emb`/`.kv` caches are used
  (not disabled), exactly like the running service.
- **Warmup excluded.** Each config runs `--warmup` (default 1) discarded run(s)
  first to warm its thread pool / model pages; warmups are never aggregated.
- **Seeded interleave.** The measured trials — every (config, sample_index)
  pair — are shuffled with a fixed `--seed` (default 0), so no config runs
  back-to-back exclusively. This controls for drift across the measured phase.
- **n ≥ 7 measured samples per config** (default `--samples 7`; rejected if a
  config can't reach that many valid samples).
- **Report median + min..max range.** No p95 — at n=7 the median is stable and
  honest; a tail percentile is not.
- **WAV validated before the metric counts.** Every emitted WAV must be
  IEEE-float32, mono, 24 kHz, non-empty, and contain only finite samples,
  otherwise the run is rejected (never half-trusted).
- **Failures are rejected, not averaged.** A non-zero exit, a timeout, a missing
  metric in the transcript, or a failed WAV validation marks that trial rejected.
  If any config ends with fewer than `--samples` valid trials, the whole run is
  flagged **incomplete** and exits non-zero.
- **SHA + args recorded.** The report records the binary's SHA-256 and the exact
  per-run args, so a result is reproducible against a specific binary build.
- **No private paths in committed output.** Recorded paths are normalized (home
  directory → `~`); the JSON report lives outside the repo.

## Phrase stratification

Timing depends on the phrase (audio length, token count), so mixing phrases into
one median conflates confound. To keep it honest:

1. **Screen** all configs at n≥7 on **one** fixed synthetic phrase (the default).
2. **Validate the winner** on a **distinct, typically longer** synthetic phrase
   in a separate run (`--text "…"`), again at n≥7, and compare the same metrics.

A winner must win on the screening phrase *and* hold on the validation phrase
before it is trusted. The report records the exact phrase used, so the two runs
are attributable.

## Metrics

| metric           | source | unit |
|------------------|--------|------|
| `rtfx`           | `RTFx: Nx` in `--profile` output | audio seconds / generation seconds (higher is faster) |
| `first_chunk_ms` | `First chunk latency: Nms` | milliseconds to first audio callback |
| `load_secs`      | `Loaded in Ns` | model load seconds |
| `wall_secs`      | measured wall-clock of the whole process | seconds (load + generate + save) |

Each run's WAV is validated (IEEE-float32, mono, 24 kHz, finite, non-empty) and
its duration is recorded from that validated WAV (`sample_count / 24000`); it is
**not** compared against the log's parsed audio duration.

## Output

Everything is written outside the repo (default is a fresh temp dir; the
documented location is `/tmp/house-performance-20260906/`):

```text
<output-dir>/benchmark-<timestamp>.json   full report (per-run + aggregates)
<output-dir>/runs/<config>-<kind><n>.log   raw transcript per trial
<output-dir>/wav/                          per-run WAVs (deleted unless --keep-wavs)
```

Exit codes: `0` complete, `1` pre-flight/usage error, `2` ran but incomplete
(some config < `--samples` valid runs).

Sanitized copies of the two finished runs (no WAV audio, no WAV/output paths,
no home paths) are committed alongside this doc at
`docs/performance/20260906/{screen,long}.json`. The raw `/tmp` transcripts/
WAVs are **not** committed.

## Results

Ran both phases on the M4 Max (this binary build, `--temperature 0 --profile`,
voice caches preserved). Every config completed n=7 measured samples per phase;
all WAVs validated (float32, mono, 24 kHz, finite, non-empty). Sanitized raw
evidence — every per-run metric and the exact args — is committed at
`docs/performance/20260906/{screen,long}.json` (no WAV audio, no WAV/output
paths, no home paths).

### Phase 1 — screen (short, fixed synthetic phrase)

`This is a local performance test. Clear speech should stay clear.` (3.98 s audio)

| config | n | RTFx min..med..max | first-ms min..med..max | load-s (med) | wall-s (med) |
|--------|---|-------------------|------------------------|--------------|--------------|
| `auto` | 7 | 22.23 .. **22.69** .. 23.60 | 20 .. **20** .. 22 | 0.17 | 0.36 |
| `2/2/4`| 7 | 22.46 .. **22.80** .. 23.18 | 20 .. **20** .. 24 | 0.17 | 0.36 |
| `3/2/2`| 7 | 21.58 .. **23.08** .. 23.46 | 20 .. **23** .. 25 | 0.17 | 0.36 |
| `2/1/2`| 7 | 18.89 .. **19.44** .. 19.64 | 23 .. **24** .. 25 | 0.17 | 0.39 |

### Phase 2 — validate (distinct, longer synthetic phrase)

`The morning meeting begins at nine. Please bring the research notes and leave
enough time for questions. This longer local test checks that clear speech
remains consistent across several sentences.` (10.45 s audio)

| config | n | RTFx min..med..max | first-ms min..med..max | load-s (med) | wall-s (med) |
|--------|---|-------------------|------------------------|--------------|--------------|
| `auto` | 7 | 24.26 .. **25.17** .. 25.84 | 20 .. **21** .. 24 | 0.18 | 0.60 |
| `2/2/4`| 7 | 25.00 .. **26.05** .. 26.60 | 20 .. **21** .. 22 | 0.17 | 0.59 |
| `3/2/2`| 7 | 24.88 .. **25.30** .. 25.73 | 20 .. **22** .. 25 | 0.17 | 0.60 |
| `2/1/2`| 7 | 20.82 .. **21.14** .. 21.42 | 24 .. **25** .. 25 | 0.18 | 0.68 |

### Verdict: keep the defaults — no production change or install justified

Across both phrases the spread among `auto`, `2/2/4`, and `3/2/2` is ≤ ~3.5% on
RTFx and ≤ ~2 ms on first chunk — sub-noise for a busy Mac, not material. The
only decisive signal is `2/1/2` (decoder = 1 thread): RTFx ~19.4x / 21.1x and
first chunk ~24 / 25 ms, clearly worse on both phases. Lowering decoder threads
hurts. So nothing in this benchmark justifies changing the production flags or
installing a new config; the shipped defaults stand.

These numbers are diagnostic for this binary/setup (short vs long phrase,
`--temperature 0 --profile`, this build). They are not directly comparable to
the ~31x / ~22 ms headline in `docs/RUNBOOK.md`, which is measured under its own
conditions.

### Caveats

- **CLI model load is separate from first callback.** `load_secs` is the model
  load time (`Loaded in ...`); `first_chunk_ms` is the CLI's first audio-callback
  latency. The first callback is **not** the HTTP-server time-to-first-audio a
  user hears — the streaming server path adds its own prewarm/chunk overhead, so
  these CLI timings understate end-to-end latency.
- Static review outcome: **PASS** — the TTS successful-run reporting is truthful.
  Two flagged items were addressed: timeouts now preserve partial stdout/stderr
  (bytes-safe decoded, unit-tested), and the doc no longer claims the parsed
  session duration is cross-checked against the WAV (it is only validated). The
  parent owns final status.

## References

- `docs/RUNBOOK.md` — benchmarking rules (`--temperature 0`, n≥5 medians, pair
  runs, never diff WAVs sample-by-sample) and the recommended
  `--threads-ar 3 --threads-dec 2 --threads-full 2` setup.
- `docs/OPTIMIZATION_NOTES.md` — the 2026-07 optimization rounds that reached
  ~31x / ~22 ms, and why the decoder is Accelerate-bound (more decoder threads
  do not help).
- `src/pocket_tts.cpp` — thread budget logic and the `--profile` output format.
