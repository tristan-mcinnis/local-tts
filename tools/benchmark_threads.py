#!/usr/bin/env python3
"""Thread-only performance benchmark for the installed local-tts binary.

Runs the installed `pocket-tts` CLI at a fixed, repeatable setup and varies
ONLY the ORT thread counts. It measures and reports per-config median + range
for:

  * RTFx              — real-time factor (audio seconds / generation seconds)
  * First chunk ms    — time to first audio callback
  * Load secs         — model load time (from the "Loaded in" line)
  * Wall secs         — total process wall-clock (includes load + generate + save)

Every benchmark is designed to be repeatable and honest:

  * `--temperature 0` so the latent sequence is deterministic — the one
    deliberate deviation from production speech (which uses 0.7), and only for
    this diagnostic.
  * `--profile` to emit RTFx + First chunk latency. All other flags stay at the
    binary's defaults, so the benchmark mirrors the running service setup on
    both sides (defaults unchanged); voice .emb/.kv caches are preserved, not
    disabled, exactly as the real service uses them.
  * ONE fixed synthetic phrase per run (overridable with `--text`), so every
    config and every sample is compared on identical input. Heterogeneous
    phrase timings are never mixed into a single figure: screen with one
    phrase at n>=7, then validate the winner on a distinct longer phrase in a
    separate run.
  * A measured warmup per config, discarded before aggregation.
  * Measured sample runs **interleaved** in a seeded-random order so no config
    runs back-to-back exclusively (controls for thermal / scheduling drift).
  * `n >= 7` measured samples per config; report **median and range**, never
    p95 at n=7.
  * The emitted WAV is validated (float32, mono, 24 kHz, finite, non-empty)
    before the metric is accepted.

Only stdlib is used (argparse, struct, array, math, statistics, subprocess,
tempfile, hashlib, json, re, pathlib, random, time).

The output directory lives OUTSIDE the repo (default under the OS temp dir) and
holds the per-run transcripts, the WAVs (deleted after validation unless
--keep-wavs), and the JSON report. Recorded paths are normalized so no home
directory leaks into the report (home -> `~`).

This script does NOT build the binary, touch runtime config, or install
anything. It only shells out to an already-installed binary.

Usage:
    python3 tools/benchmark_threads.py [--output-dir /tmp/...] [--dry-run]

Exit codes:
    0  all configs produced >= --samples valid samples
    1  usage error or pre-flight validation failed (binary/model/voice missing)
    2  benchmark ran but produced incomplete results (some config had < samples)
"""

from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import os
import random
import re
import statistics
import struct
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Installed binary (runtime layout, see CLAUDE.md "Runtime layout"). The repo
# root `pocket-tts` / `models/` is a build/development layout; the installed
# binary under ~/.local/libexec is what the launchd service actually runs.
DEFAULT_BINARY = Path("~/.local/libexec/local-tts/pocket-tts").expanduser()
DEFAULT_MODELS = Path("~/Models/pocket-tts").expanduser()
DEFAULT_VOICES = DEFAULT_MODELS / "voices"
DEFAULT_VOICE = "example.wav"

# A single fixed synthetic phrase. `--text` overrides it. One phrase per run so
# configs/samples are compared on identical input; screen on this, then validate
# the winner on a distinct longer phrase in a separate run (never mix timings).
DEFAULT_PHRASE = (
    "This is a local performance test. Clear speech should stay clear."
)

# Thread configs. `name -> (threads_ar, threads_dec, threads_full)`; None means
# "auto" = pass no thread flags, so the binary applies its own derived budget
# (--threads 0 = half cores, auto-balanced across ar/dec/full).
THREAD_SPECS = {
    "auto": None,
    "2/2/4": (2, 2, 4),
    "3/2/2": (3, 2, 2),
    "2/1/2": (2, 1, 2),
}
DEFAULT_CONFIGS = ["auto", "2/2/4", "3/2/2", "2/1/2"]

# Flags that stay identical across every config; only the thread flags vary.
# Reproduces the running service's defaults on both sides, plus the two
# diagnostic additions (temperature 0 for determinism, --profile for the
# metrics). No --no-cache / --low-latency / --trim-* overrides: defaults apply.
BASE_FLAGS = [
    "--temperature", "0",
    "--profile",
]

# Required model files to run the default fast path (used for a pre-flight
# warning only; the binary falls back to older graphs if one is absent).
REQUIRED_MODEL_GLOBS = ["flow_lm_main*.onnx", "mimi_decoder*.onnx"]
WARN_MODEL_GLOBS = {
    "flow_lm_main_delta_attn_flow_int8.onnx": "merged delta-KV AR -> RTFx drop on fallback",
    "mimi_decoder_delta_convtr_int8.onnx": "fused Apple decoder -> RTFx drop on fallback",
}


# ---------------------------------------------------------------------------
# Small helpers (pure, importable, tested)
# ---------------------------------------------------------------------------

def normalize_text(text: str, home: str | None = None) -> str:
    """Replace an absolute home prefix with `~` so private paths never leak."""
    home = home or str(Path.home())
    if text.startswith(home):
        rest = text[len(home):]
        if rest.startswith(os.sep):
            return "~" + rest
        # e.g. exactly the home dir, or a prefix like /Users/tristanX
    return text


def normalize_path(path, home: str | None = None) -> str:
    return normalize_text(os.path.normpath(str(path)), home=home)


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def thread_flags_for_spec(spec):
    """Map a thread spec tuple to CLI flags. None (auto) -> no thread flags."""
    if spec is None:
        return []
    ar, dec, full = spec
    return [
        "--threads-ar", str(ar),
        "--threads-dec", str(dec),
        "--threads-full", str(full),
    ]


def build_command(binary, base_flags, thread_flags, models_dir, tokenizer,
                  voices_dir, text, voice, output, extra_flags=()):
    """Assemble the pocket-tts command. Positionals are TEXT VOICE OUTPUT."""
    cmd = [str(binary)]
    cmd += list(extra_flags)
    cmd += list(base_flags)
    cmd += [
        "--models-dir", str(models_dir),
        "--tokenizer", str(tokenizer),
        "--voices-dir", str(voices_dir),
    ]
    cmd += list(thread_flags)
    cmd += [text, voice, str(output)]
    return cmd


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

_RTFX_RE = re.compile(r"RTFx:\s*([\d.]+)\s*x")
_LOAD_RE = re.compile(r"Loaded in ([\d.]+)\s*s")
_FCM_RE = re.compile(r"First chunk latency:\s*([\d.]+)\s*ms")
_GEN_RE = re.compile(r"([\d.]+)\s*s audio in ([\d.]+)\s*s")


def parse_log(text: str) -> dict:
    """Pull the benchmark metrics out of a combined stdout+stderr transcript.

    Returns a dict with keyword metrics; values are None when the marker was
    not found. A run with all three (rtfx/load_secs/first_chunk_ms) present is
    considered parse-usable by `is_usable_metrics`.
    """
    def g(regex):
        m = regex.search(text)
        return float(m.group(1)) if m else None

    m = _GEN_RE.search(text)
    audio_dur_s = float(m.group(1)) if m else None
    gen_s = float(m.group(2)) if m else None
    return {
        "rtfx": g(_RTFX_RE),
        "load_secs": g(_LOAD_RE),
        "first_chunk_ms": g(_FCM_RE),
        "audio_duration_s": audio_dur_s,
        "gen_secs": gen_s,
    }


def is_usable_metrics(parsed: dict) -> bool:
    """A run is usable only when the three metrics we report are all present."""
    return (
        parsed.get("rtfx") is not None
        and parsed.get("load_secs") is not None
        and parsed.get("first_chunk_ms") is not None
    )


# ---------------------------------------------------------------------------
# WAV validation
# ---------------------------------------------------------------------------

def parse_wav(path) -> dict:
    """Validate the emitted WAV and return its properties.

    Raises ValueError for anything that fails: not RIFF/WAVE, not IEEE-float32,
    not mono, not 24 kHz, empty, or containing non-finite samples.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    off = 12
    fmt = None
    data_off = None
    data_len = None
    while off + 8 <= len(data):
        cid = data[off:off + 4]
        csz = struct.unpack("<I", data[off + 4:off + 8])[0]
        if cid == b"fmt " and fmt is None:
            payload = data[off + 8:off + 8 + 16]
            if len(payload) < 16:
                raise ValueError("truncated fmt chunk")
            af, ch, sr, br, ba, bps = struct.unpack("<HHIIHH", payload)
            fmt = dict(format=af, channels=ch, rate=sr, byte_rate=br,
                       block_align=ba, bits=bps)
        elif cid == b"data":
            data_off = off + 8
            data_len = csz
            break
        off += 8 + csz + (csz & 1)
    if fmt is None or data_off is None:
        raise ValueError("missing fmt/data chunk")
    if fmt["format"] != 3 or fmt["bits"] != 32:
        raise ValueError(
            "not IEEE-float32 wav (format=%s bits=%s)" % (fmt["format"], fmt["bits"]))
    if fmt["channels"] != 1:
        raise ValueError("not monophonic wav (channels=%s)" % fmt["channels"])
    if fmt["rate"] != 24000:
        raise ValueError("not 24 kHz wav (rate=%s)" % fmt["rate"])
    bytes_per_sample = (fmt["bits"] // 8) * fmt["channels"]
    sample_count = data_len // bytes_per_sample
    if sample_count == 0:
        raise ValueError("empty wav (0 samples)")
    samples = array.array("f")
    raw = data[data_off:data_off + data_len]
    if sys.byteorder == "big":
        samples.frombytes(raw)
        samples.byteswap()
    else:
        samples.frombytes(raw)
    finite = all(math.isfinite(x) for x in samples)
    if not finite:
        raise ValueError("non-finite sample values present")
    return {
        "format": fmt["format"],
        "channels": fmt["channels"],
        "rate": fmt["rate"],
        "bits": fmt["bits"],
        "block_align": fmt["block_align"],
        "sample_count": sample_count,
        "duration_s": sample_count / fmt["rate"],
    }


# ---------------------------------------------------------------------------
# Aggregation (median + range, never p95 at n=7)
# ---------------------------------------------------------------------------

def aggregate(sample_runs, required_samples: int, configs=None):
    """Aggregate valid sample runs into per-config stats.

    `sample_runs` is a list of run records (all kind == "sample", ok True).
    `configs` is the full list of benchmarked config names; any config with
    fewer than required_samples valid runs here is reported as incomplete
    (including a config with zero valid runs, which would otherwise vanish).

    Returns (results_by_config, complete) where complete is True only when every
    requested config has >= required_samples valid runs.
    """
    by_config = defaultdict(list)
    for r in sample_runs:
        by_config[r["config"]].append(r)

    if configs is None:
        configs = list(by_config.keys())

    results = {}
    for cfg in configs:
        runs = by_config.get(cfg, [])
        metrics = {k: [] for k in ("rtfx", "first_chunk_ms", "load_secs", "wall_secs")}
        for r in runs:
            for k in metrics:
                v = r.get(k)
                if v is not None:
                    metrics[k].append(v)
        metrics_out = {}
        for k, vals in metrics.items():
            if not vals:
                metrics_out[k] = None
                continue
            s = sorted(vals)
            metrics_out[k] = {
                "median": statistics.median(s),
                "min": s[0],
                "max": s[-1],
                "n": len(s),
            }
        results[cfg] = {
            "config": cfg,
            "spec": THREAD_SPECS.get(cfg, "unknown"),
            "samples_reported": len(runs),
            "complete": len(runs) >= required_samples,
            "metrics": metrics_out,
        }

    complete = all(res["complete"] for res in results.values())
    return results, complete


def build_trial_plan(configs, warmup: int, samples: int, seed: int):
    """Return (warmup_trials, measured_trials).

    Warmups run first (one per config, `warmup` times each, config order) and are
    discarded. Measured trials mix every (config, sample_index) and are shuffled
    with `seed` so configs interleave for the whole measured phase.
    """
    warmup_trials = []
    for cfg in configs:
        for w in range(warmup):
            warmup_trials.append({"config": cfg, "kind": "warmup", "index": w})
    measured = []
    for cfg in configs:
        for i in range(samples):
            measured.append({"config": cfg, "kind": "sample", "index": i})
    rng = random.Random(seed)
    rng.shuffle(measured)
    return warmup_trials, measured


# ---------------------------------------------------------------------------
# Pre-flight validation
# ---------------------------------------------------------------------------

def validate_prereqs(binary, models_dir, tokenizer, voices_dir, voice) -> list:
    """Return a list of (kind, message) problems; empty == ready to run."""
    problems = []
    if not binary.exists() or not os.access(binary, os.X_OK):
        problems.append(("error", "installed binary not found/executable: %s" % binary))
    if not models_dir.is_dir():
        problems.append(("error", "models dir missing: %s" % models_dir))
    else:
        if not tokenizer.exists():
            problems.append(("error", "tokenizer missing: %s" % tokenizer))
        for g in REQUIRED_MODEL_GLOBS:
            if not list(models_dir.glob(g)):
                problems.append(("warning", "no %s in %s" % (g, models_dir)))
        for fname, why in WARN_MODEL_GLOBS.items():
            if models_dir.is_dir() and not (models_dir / fname).exists():
                problems.append(("warning", "%s absent -> %s" % (fname, why)))
    if not voices_dir.is_dir():
        problems.append(("error", "voices dir missing: %s" % voices_dir))
    elif not (voices_dir / voice).exists():
        problems.append(("error", "voice file missing: %s" % (voices_dir / voice)))
    return problems


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _try_decode(data):
    """Bytes-safe-decode partial subprocess output; None -> ''."""
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return str(data)


def _run_trial(params, cmd, output_path, timeout, run_label, log_dir):
    """Run one pocket-tts invocation and return a run record."""
    record = {
        "config": params["config"],
        "kind": params["kind"],
        "index": params["index"],
        "phrase": params["phrase"],
        "label": run_label,
        "ok": False,
        "timeout": False,
        "exit_code": None,
        "error": None,
        "rtfx": None,
        "first_chunk_ms": None,
        "load_secs": None,
        "wall_secs": None,
        "audio_duration_s": None,
    }
    transcript = ""
    cmd_disp = [normalize_text(c) for c in cmd]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        wall = time.monotonic() - t0
        transcript = (proc.stdout or "") + (proc.stderr or "")
        record["exit_code"] = proc.returncode
        record["wall_secs"] = wall
        if proc.returncode != 0:
            raise RuntimeError("exit code %s" % proc.returncode)
        parsed = parse_log(transcript)
        if not is_usable_metrics(parsed):
            raise RuntimeError("missing benchmark metrics (rtfx/load/first_chunk)")
        wav = parse_wav(output_path)
        record["rtfx"] = parsed["rtfx"]
        record["load_secs"] = parsed["load_secs"]
        record["first_chunk_ms"] = parsed["first_chunk_ms"]
        record["audio_duration_s"] = wav["duration_s"]
        record["wav_ok"] = {
            "channels": wav["channels"],
            "rate": wav["rate"],
            "bits": wav["bits"],
            "sample_count": wav["sample_count"],
        }
        record["ok"] = True
    except subprocess.TimeoutExpired as exc:
        # Preserve whatever the process wrote before it was killed; TimeoutExpired
        # carries it as bytes even when text=True was requested.
        record["timeout"] = True
        record["wall_secs"] = time.monotonic() - t0
        record["error"] = "timed out after %ss" % timeout
        partial = _try_decode(exc.stdout) + _try_decode(exc.stderr)
        transcript = ("TIMEOUT after %ss\n" % timeout) + (partial if partial else "")
    except Exception as exc:  # noqa: BLE001 - record any failure
        record["error"] = str(exc)

    # Archive the raw transcript for the trial so failures are auditable.
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / (run_label + ".log")
        try:
            log_path.write_text(
                "# %s - benchmark transcript\n# cwd: %s\n# cmd: %s\n%s\n"
                % (run_label, normalize_text(str(Path.cwd())), " ".join(cmd_disp), transcript)
            )
        except OSError:
            pass
    return record


def benchmark(cfg):
    """Execute the full benchmark and return the JSON report dict."""
    home = str(Path.home())
    output_dir = Path(cfg.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    wav_dir = output_dir / "wav"
    wav_dir.mkdir(exist_ok=True)
    log_dir = output_dir / "runs"

    binary = Path(cfg.binary).expanduser()
    models_dir = Path(cfg.models_dir).expanduser()
    voices_dir = Path(cfg.voices_dir).expanduser()
    tokenizer = Path(cfg.tokenizer).expanduser() if cfg.tokenizer else models_dir / "tokenizer.model"
    voice = cfg.voice

    prereqs = validate_prereqs(binary, models_dir, tokenizer, voices_dir, voice)
    errors = [p for k, p in prereqs if k == "error"]
    for kind, msg in prereqs:
        print("[%s] %s" % (kind.upper(), msg), file=sys.stderr)
    if errors:
        print("FAIL: pre-flight validation failed.", file=sys.stderr)
        return {"_exit": 1, "prereq_problems": prereqs}

    configs = cfg.configs.split(",") if isinstance(cfg.configs, str) else cfg.configs
    configs = [c.strip() for c in configs if c.strip()]
    configs = [c for c in configs if c in THREAD_SPECS]
    if not configs:
        print("FAIL: no valid config names (pick from %s)" % ",".join(THREAD_SPECS),
              file=sys.stderr)
        return {"_exit": 1}

    phrase = cfg.text if cfg.text else DEFAULT_PHRASE
    extra = list(cfg.extra)
    warmup, measured = build_trial_plan(configs, cfg.warmup, cfg.samples, cfg.seed)

    # Human-readable representation of each trial for the report.
    def trial_label(tr):
        return "%s-%s-%d" % (tr["config"].replace("/", "_"), tr["kind"], tr["index"])

    run_records = []

    # Warmups first (discarded), config order. Same fixed phrase as the
    # measured phase.
    for tr in warmup:
        output_path = wav_dir / ("%s.wav" % trial_label(tr))
        spec = THREAD_SPECS[tr["config"]]
        thread_flags = thread_flags_for_spec(spec)
        cmd = build_command(binary, BASE_FLAGS, thread_flags, models_dir,
                            tokenizer, voices_dir, phrase, voice, output_path, extra)
        if cfg.verbose:
            print("WARMUP %s: %s" % (trial_label(tr), " ".join(normalize_text(c) for c in cmd)),
                  file=sys.stderr)
        rec = _run_trial({**tr, "phrase": phrase}, cmd, output_path, cfg.timeout,
                         trial_label(tr), log_dir)
        record_meta = dict(rec)
        record_meta["cmd"] = [normalize_text(c) for c in cmd]
        run_records.append(record_meta)

    # Measured samples, seeded-interleaved (all on the one fixed phrase).
    for tr in measured:
        output_path = wav_dir / ("%s.wav" % trial_label(tr))
        spec = THREAD_SPECS[tr["config"]]
        thread_flags = thread_flags_for_spec(spec)
        cmd = build_command(binary, BASE_FLAGS, thread_flags, models_dir,
                            tokenizer, voices_dir, phrase, voice, output_path, extra)
        if cfg.verbose:
            print("RUN %s: %s" % (trial_label(tr), " ".join(normalize_text(c) for c in cmd)),
                  file=sys.stderr)
        rec = _run_trial({**tr, "phrase": phrase}, cmd, output_path, cfg.timeout,
                         trial_label(tr), log_dir)
        record_meta = dict(rec)
        record_meta["cmd"] = [normalize_text(c) for c in cmd]
        run_records.append(record_meta)
        if rec["ok"]:
            print("  ok   %s  RTFx=%s  first_ms=%s  load=%ss  wall=%ss"
                  % (trial_label(tr), rec["rtfx"], rec["first_chunk_ms"],
                     "%.2f" % rec["load_secs"], "%.2f" % rec["wall_secs"]))
        else:
            print("  FAIL %s  (%s)" % (trial_label(tr), rec["error"] or "unknown"),
                  file=sys.stderr)

    # Delete WAVs unless asked to keep them.
    if not cfg.keep_wavs:
        for tr in warmup + measured:
            p = wav_dir / ("%s.wav" % trial_label(tr))
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    sample_runs = [r for r in run_records if r["kind"] == "sample" and r["ok"]]
    results, complete = aggregate(sample_runs, cfg.samples, configs)

    report = {
        "schema": "local-tts-thread-benchmark",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "binary": {
            "path": normalize_path(binary, home),
            "sha256": sha256_file(binary),
            "size_bytes": binary.stat().st_size,
        },
        "config": {
            "configs": configs,
            "phrases": [phrase],
            "voice": voice,
            "models_dir": normalize_path(models_dir, home),
            "voices_dir": normalize_path(voices_dir, home),
            "tokenizer": normalize_path(tokenizer, home),
            "output_dir": normalize_path(output_dir, home),
            "base_flags": BASE_FLAGS,
            "extra_flags": extra,
            "warmup": cfg.warmup,
            "samples": cfg.samples,
            "seed": cfg.seed,
            "timeout_secs": cfg.timeout,
            "interleaved": True,
        },
        "thread_specs": {
            name: {
                "flags": thread_flags_for_spec(spec),
                "spec": spec,
            } for name, spec in THREAD_SPECS.items()
        },
        "complete": complete,
        "partial": incomplete_configs(results),
        "results": results,
        "runs": run_records,
    }

    json_path = cfg.json_out
    if not json_path:
        json_path = output_dir / ("benchmark-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    json_path = Path(json_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2))

    if cfg.json_out:
        print("Report: %s" % normalize_path(json_path, home))

    # Console summary (medians + range only).
    print("\n===== SUMMARY (median, range; n>=%d) =====" % cfg.samples)
    header = "%-10s %-5s %14s %16s %14s %14s %s" % (
        "config", "n", "RTFx (min..med..max)", "first_ms", "load_s", "wall_s", "status")
    print(header)
    for name in configs:
        res = results.get(name)
        if res is None:
            print("%-10s %-5s   insufficient valid samples" % (name, 0))
            continue
        m = res["metrics"]
        rtfx = m.get("rtfx")
        fcm = m.get("first_chunk_ms")
        load = m.get("load_secs")
        wall = m.get("wall_secs")

        def fmt(stats, nd=2):
            if stats is None:
                return "-"
            return "%s..%s..%s" % (round(stats["min"], nd), round(stats["median"], nd),
                                   round(stats["max"], nd))

        print("%-10s %-5d %20s %16s %14s %14s %s" % (
            name, res["samples_reported"],
            fmt(rtfx), fmt(fcm, 1), fmt(load), fmt(wall),
            "OK" if res["complete"] else "INCOMPLETE"))

    if not complete:
        print("\nWARNING: benchmark incomplete; some configs had < %d valid samples."
              % cfg.samples, file=sys.stderr)

    return report


def incomplete_configs(results):
    return [name for name, res in results.items() if not res["complete"]]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Thread-only benchmark for the installed local-tts binary.")
    p.add_argument("--binary", default=str(DEFAULT_BINARY),
                   help="installed pocket-tts binary (default %s)" % DEFAULT_BINARY)
    p.add_argument("--models-dir", default=str(DEFAULT_MODELS),
                   help="model directory (default %s)" % DEFAULT_MODELS)
    p.add_argument("--voices-dir", default=str(DEFAULT_VOICES),
                   help="voices directory (default %s)" % DEFAULT_VOICES)
    p.add_argument("--voice", default=DEFAULT_VOICE,
                   help="voice filename in voices-dir (default %s)" % DEFAULT_VOICE)
    p.add_argument("--tokenizer", default=None,
                   help="tokenizer model path (default <models-dir>/tokenizer.model)")
    p.add_argument("--output-dir", default=None,
                   help="output directory (default a fresh temp dir under the OS temp dir)")
    p.add_argument("--json-out", default=None, help="path to write the JSON report")
    p.add_argument("--configs", default=",".join(DEFAULT_CONFIGS),
                   help="comma-separated config names (default %s)" % ",".join(DEFAULT_CONFIGS))
    p.add_argument("--text", default=None,
                   help="one fixed synthetic phrase (default: the built-in screening phrase)")
    p.add_argument("--samples", type=int, default=7, help="measured samples per config (>=7)")
    p.add_argument("--warmup", type=int, default=1, help="discarded warmup runs per config")
    p.add_argument("--seed", type=int, default=0, help="seed for interleave ordering")
    p.add_argument("--timeout", type=float, default=120.0,
                   help="per-run timeout in seconds (default 120)")
    p.add_argument("--extra", action="append", default=[],
                   help="extra constant flag appended to base flags (repeatable)")
    p.add_argument("--keep-wavs", action="store_true",
                   help="keep the per-run WAV files instead of deleting them")
    p.add_argument("--verbose", action="store_true", help="echo each run command")
    p.add_argument("--dry-run", action="store_true",
                   help="print the trial plan and commands without executing")
    return p.parse_args(argv)


def main(argv=None):
    cfg = parse_args(argv if argv is not None else sys.argv[1:])
    home = str(Path.home())
    binary = Path(cfg.binary).expanduser()
    models_dir = Path(cfg.models_dir).expanduser()
    voices_dir = Path(cfg.voices_dir).expanduser() if cfg.voices_dir else DEFAULT_VOICES
    tokenizer = Path(cfg.tokenizer).expanduser() if cfg.tokenizer else models_dir / "tokenizer.model"

    if cfg.output_dir is None:
        cfg.output_dir = tempfile.mkdtemp(prefix="local-tts-thread-bench-")

    if cfg.samples < 7:
        print("FAIL: --samples must be >= 7 to report a median (got %d)" % cfg.samples,
              file=sys.stderr)
        return 1

    configs = [c.strip() for c in cfg.configs.split(",") if c.strip()]
    unknown = [c for c in configs if c not in THREAD_SPECS]
    if unknown:
        print("FAIL: unknown config name(s): %s (pick from %s)"
              % (", ".join(unknown), ",".join(THREAD_SPECS)), file=sys.stderr)
        return 1

    if cfg.dry_run:
        phrase = cfg.text if cfg.text else DEFAULT_PHRASE
        warmup, measured = build_trial_plan(configs, cfg.warmup, cfg.samples, cfg.seed)
        print("Dry run (no execution). output-dir: %s" % cfg.output_dir)
        print("binary: %s" % normalize_path(binary, home))
        print("models: %s" % normalize_path(models_dir, home))
        print("voices: %s  voice: %s" % (normalize_path(voices_dir, home), cfg.voice))
        print("configs: %s  warmup=%d samples=%d seed=%d timeout=%ss"
              % (configs, cfg.warmup, cfg.samples, cfg.seed, cfg.timeout))
        print("base flags: %s" % " ".join(BASE_FLAGS + list(cfg.extra)))
        print("phrase: %s" % phrase)
        print("\nWarmup trials:")
        for tr in warmup:
            cmd = build_command(binary, BASE_FLAGS, thread_flags_for_spec(THREAD_SPECS[tr["config"]]),
                                models_dir, tokenizer, voices_dir, phrase, cfg.voice,
                                str(Path(cfg.output_dir) / (tr["config"].replace("/", "_") + "-warmup%d.wav" % tr["index"])),
                                cfg.extra)
            print("  %-10s %s" % (tr["config"], " ".join(normalize_text(c) for c in cmd)))
        print("\nMeasured trials (seeded interleave, order as listed):")
        for tr in measured:
            cmd = build_command(binary, BASE_FLAGS, thread_flags_for_spec(THREAD_SPECS[tr["config"]]),
                                models_dir, tokenizer, voices_dir, phrase, cfg.voice,
                                str(Path(cfg.output_dir) / (tr["config"].replace("/", "_") + "-sample%d.wav" % tr["index"])),
                                cfg.extra)
            print("  %-10s s%d  %s" % (tr["config"], tr["index"],
                                       " ".join(normalize_text(c) for c in cmd)))
        return 0

    report = benchmark(cfg)
    if "_exit" in report:
        return report["_exit"]
    return 0 if report.get("complete") else 2


if __name__ == "__main__":
    sys.exit(main())
