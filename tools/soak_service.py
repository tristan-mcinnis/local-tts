#!/usr/bin/env python3
"""Repeated-use memory probe for the installed local-tts HTTP server.

This is the follow-up to `benchmark_threads.py` (throughput) and the sibling of
the CLI benchmark. Its job is the OTHER axis from the thread benchmark: does the
SERVICE process memory grow over repeated use? It runs an ISOLATED `pocket-tts
--server` instance against a fresh, temp voices-directory copy and sends a batch
of synthetic requests, sampling the *server process's own* **physical footprint**
(`footprint` `phys_footprint`, which accounts for compressed + shared pages) and
RSS at fixed checkpoints. It records the *slope* between checkpoints; it does
NOT declare a leak. Early growth is the voice cache + first-render warm; a flat
plateau afterwards is the healthy signal.

## Isolation guarantees (these are load-bearing; do not weaken)

* NEVER the production service (launchd `com.tristan.local-tts`, `127.0.0.1:8081`).
  We spawn our OWN process and talk only to 127.0.0.1 on an ephemeral `--port`.
* LOOPBACK-ONLY BIND, verified at runtime. The binary now binds `127.0.0.1` by
  default (the bind fix). Before any request, the probe confirms via `lsof` that
  the single LISTEN socket on our port is bound to a loopback address AND owned
  by our spawned pid — a `*`/all-interface bind or a foreign owner is a hard
  blocker, never a caveat. /health alone is insufficient (a bind race could hand
  the port to another process, and listening only proves *some* process is up).
  The INSTALLED binary (pre-fix) still binds all interfaces, so the probe will
  refuse to run against it: pass `--binary <freshly-built-loopback binary>`.
* NEVER the production voices dir. The voice (stock `example.wav`) is COPIED
  into a fresh temp dir, and `--voices-dir` points there, so the `.emb`/`.kv`
  caches (written under `voices/<...>/.cache/`) land in TEMP, not production.
  `--models-dir` / `--tokenizer` still point at the production (read-only)
  model store — the runtime only reads models, never writes them.
* Same production flags. We pass `--server --port <port>` plus the three path
  overrides (`--models-dir`, `--tokenizer`, `--voices-dir`). NOTHING else:
  no `--temperature`, no `--threads*`, no `--trim*`, no `--no-cache`, no
  `--low-latency`. Defaults (temperature 0.7, auto thread budget, caches ON)
  mirror the running service. We deliberately do NOT pass `--profile` (the
  production service does not, and profiling buffers are not part of its
  steady-state footprint).
* Graceful teardown of OUR OWN pid only. On every exit path we SIGTERM the
  spawned pid, wait a bounded grace, then SIGKILL only that pid. We never
  `pkill`, never kill a process group, never touch the production job.

## What it measures

* Startup: model load (this is the big one-off allocation, not a leak).
* After warmup (default 3 requests, same stock voice) -> baseline sample.
* At each fixed checkpoint request-count (default 10 / 50 / 100) -> sample.
* Per request: status, HTTP error parsed (JSON `{"error":{"message","type"}}`
  for `/v1/audio/speech` 400s), timeout flag, wall latency, and the returned WAV
  validated (IEEE-float32, mono, 24 kHz, finite, non-empty) with its duration.
* Memory slope in KB-per-request between consecutive checkpoints, for both
  physical footprint and RSS.

It is a measurement harness only: no build, no runtime-config change, no
install. The 15-minute background soak is run by the parent as a SEPARATE command
after this code is reviewed — this file does not start the soak by itself.

Only stdlib is used (argparse, json, array, math, os, struct, subprocess, sys,
tempfile, time, http.client, pathlib, re, socket, shutil).

Usage:
    python3 tools/soak_service.py [--output-dir /tmp/...] [--dry-run]
    python3 tools/soak_service.py --requests 100 --checkpoints 10,50,100 \
        --output-dir /tmp/house-soak-20260906 --dry-run

Exit codes:
    0  reached every checkpoint; all checkpoints sampled; >= --min-ok-samples
       valid requests (or no valid requests were required / output only)
    1  usage / pre-flight error (binary, models, voice, footprint missing)
    2  ran but bounded soak was incomplete (some checkpoint not reached, or
       fewer than --min-ok-samples valid requests, or another non-0 issue)
"""

from __future__ import annotations

import argparse
import array
import http.client
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Installed binary (runtime layout, see CLAUDE.md "Runtime layout"). The repo
# root `pocket-tts` is a build/dev copy; the launchd service runs this one.
DEFAULT_BINARY = Path("~/.local/libexec/local-tts/pocket-tts").expanduser()
DEFAULT_MODELS = Path("~/Models/pocket-tts").expanduser()
DEFAULT_VOICES = DEFAULT_MODELS / "voices"   # SOURCE of the stock voice to copy
DEFAULT_VOICE = "example.wav"

# A single fixed synthetic, non-personal phrase. `--text` overrides it. One
# phrase per run so every request is comparable; never aggregate across phrases.
DEFAULT_PHRASE = (
    "This is a periodic memory probe of the local TTS service. "
    "It should stay clear and steady."
)

# Server-mode flags common to every run. By construction these are exactly the
# four the service differs on (port + three path overrides). Everything else
# stays at the binary's production defaults (temperature 0.7, auto threads,
# caches ON, no trimming, no low-latency, no profile).
SERVER_FLAGS = ["--server"]

# Production-flag names we must NEVER add to the probe (isolation guard). The
# service runs on these defaults; the probe mirrors them by passing NOTHING.
FORBIDDEN_FLAG_PREFIXES = (
    "--temperature", "--threads", "--trim-", "--no-cache", "--low-latency",
    "--fast-start", "--no-", "--temp", "--lsd", "--eos-", "--noise-clamp",
    "--first-chunk", "--max-chunk", "--combine-first-step", "--ort-profile-dir",
)

DEFAULT_CHECKPOINTS = [10, 50, 100]
DEFAULT_REQUESTS = 100
DEFAULT_WARMUP = 3

# Bounded run ceilings. The soak must never exceed these regardless of config.
DEFAULT_MAX_SECONDS = 900       # 15 min hard wall
DEFAULT_REQUEST_TIMEOUT = 60.0  # per-request HTTP timeout
DEFAULT_START_TIMEOUT = 120.0   # must answer /health within this after spawn
DEFAULT_READY_POLL = 0.25       # /health poll interval
DEFAULT_TERM_GRACE = 10.0       # SIGTERM -> SIGKILL grace for our own pid


# ---------------------------------------------------------------------------
# Pure helpers (importable + unit-tested; no exec, no HTTP)
# ---------------------------------------------------------------------------

def normalize_text(text: str, home: str | None = None) -> str:
    home = home or str(Path.home())
    if text.startswith(home):
        rest = text[len(home):]
        if rest.startswith(os.sep):
            return "~" + rest
    return text


def normalize_path(path, home: str | None = None) -> str:
    return normalize_text(os.path.normpath(str(path)), home=home)


def sha256_file(path) -> str:
    """SHA-256 of a file; used to make a result reproducible against a build."""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pick_free_port(bind=("127.0.0.1", 0)) -> int:
    """Ask the OS for a free ephemeral port, then release it.

    The pocket-tts binary accepts only `--port` (it binds loopback by default
    after the bind fix; there is no fd-injection and no `--bind`). We therefore
    reserve-then-release and let the server re-bind. There is an inherent tiny
    TOCTOU window between our close and the server's bind; SO_REUSEADDR is set by
    the binary, and the run's lsof ownership gate catches a lost race (foreign
    pid on the port, or the server exits before binding).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(bind)
        return s.getsockname()[1]


def forbidden_flags(argv) -> list:
    """Return any argv entry that deviates from production semantics.

    The probe must mirror the running service: it passes only `--server --port`
    plus the three path overrides. Any temperature/threads/trim/no-cache/
    low-latency/etc. flag is off the production defaults and must never reach the
    isolated server command. This is the isolation guard (pure + unit-tested).
    """
    return [a for a in argv if any(a.startswith(p) for p in FORBIDDEN_FLAG_PREFIXES)]


def build_server_command(binary, port, models_dir, tokenizer, voices_dir,
                         server_flags=None, extra_flags=()):
    """Assemble the isolated `pocket-tts --server` command.

    Only the port + the three path overrides differ from production. `extra_flags`
    is intentionally kept out of the default path so nothing can drift the probe
    off production semantics by accident.
    """
    cmd = [str(binary)]
    cmd += list(server_flags if server_flags is not None else SERVER_FLAGS)
    cmd += list(extra_flags)
    cmd += [
        "--port", str(int(port)),
        "--models-dir", str(models_dir),
        "--tokenizer", str(tokenizer),
        "--voices-dir", str(voices_dir),
    ]
    return cmd


def make_temp_voices_dir(src_voice, root=None) -> tuple[Path, Path]:
    """Copy ONLY the stock voice into a fresh temp dir; return (dir, voice).

    We copy the single voice file (no `.cache/`, no other voices) so the server
    starts cold and writes its own `.emb`/`.kv` under the TEMP
    `<voices>/.cache/` — production `voices/.cache/` is never touched.
    """
    src_voice = Path(src_voice)
    if not src_voice.exists():
        raise FileNotFoundError("source voice not found: %s" % src_voice)
    base = Path(root) if root else None
    if base is None:
        base = Path(tempfile.mkdtemp(prefix="local-tts-soak-"))
    elif not base.exists():
        base.mkdir(parents=True, exist_ok=True)
    voices_dir = base / "voices"
    voices_dir.mkdir(parents=True, exist_ok=True)
    dest_voice = voices_dir / src_voice.name
    shutil.copyfile(src_voice, dest_voice)
    return voices_dir, dest_voice


def health_url(port) -> str:
    return "http://127.0.0.1:%d/health" % int(port)


def speech_url(port) -> str:
    return "http://127.0.0.1:%d/v1/audio/speech" % int(port)


def build_speech_payload(text, voice, response_format="wav") -> bytes:
    """OpenAI-compatible TTS request body (model + speed are accepted, ignored)."""
    return json.dumps({
        "model": "local-tts",
        "input": text,
        "voice": voice,
        "response_format": response_format,
    }).encode("utf-8")


def text_for_request(index, base, mode="varied") -> str:
    """Deterministic per-request text for the measured phase.

    The voice cache (.emb/.kv) is per-VOICE and text-independent, so a fixed key
    would just replay one cached decode and never exercise fresh AR generation.
    To make the footprint trend meaningful we vary the text across a SHORT fixed
    template (the base phrase plus the request number), so output length stays
    roughly constant and the run stays bounded. `varied` embeds the request
    number; `fixed` always returns `base` (e.g. for a same-text control).

    Generates a NEW string each call so the text genuinely differs per request
    (not a frozen list), deterministically from `index`.
    """
    if mode == "fixed":
        return base
    return "%s [run %d]" % (base, int(index))


# ---------------------------------------------------------------------------
# WAV validation (in-memory; bytes -> properties). Mirrors benchmark_threads.parse_wav
# but operates on a bytes object so the probe writes no per-request temp files.
# ---------------------------------------------------------------------------

def validate_wav_bytes(data: bytes) -> dict:
    """Validate the /v1/audio/speech `wav` body. Raises ValueError on any problem.

    The server's wav_encode writes IEEE-float32, mono, 24 kHz, 32-bit. We require
    exactly that, plus non-empty and all-finite samples.
    """
    if not isinstance(data, bytes) or len(data) < 44:
        raise ValueError("not a RIFF/WAVE file (too short to hold a header)")
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
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
        raise ValueError("not IEEE-float32 wav (format=%s bits=%s)"
                         % (fmt["format"], fmt["bits"]))
    if fmt["channels"] != 1:
        raise ValueError("not monophonic wav (channels=%s)" % fmt["channels"])
    if fmt["rate"] != 24000:
        raise ValueError("not 24 kHz wav (rate=%s)" % fmt["rate"])
    bytes_per_sample = (fmt["bits"] // 8) * fmt["channels"]
    sample_count = data_len // bytes_per_sample
    if sample_count == 0:
        raise ValueError("empty wav (0 samples)")
    raw = data[data_off:data_off + data_len]
    samples = array.array("f")
    samples.frombytes(raw)
    if sys.byteorder == "big":
        samples.byteswap()
    if not all(math.isfinite(x) for x in samples):
        raise ValueError("non-finite sample values present")
    return {
        "format": fmt["format"], "channels": fmt["channels"],
        "rate": fmt["rate"], "bits": fmt["bits"],
        "block_align": fmt["block_align"], "sample_count": sample_count,
        "duration_s": sample_count / fmt["rate"],
    }


# ---------------------------------------------------------------------------
# Memory sampling for OUR OWN pid (not the whole machine, not "RSS only")
# ---------------------------------------------------------------------------

_PS_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s*$")
_FP_HEAD_RE = re.compile(r"Footprint:\s*([\d.]+)\s*(KB|MB|GB)", re.IGNORECASE)
_FP_PHYS_RE = re.compile(r"phys_footprint:\s*([\d.]+)\s*(KB|MB|GB)", re.IGNORECASE)
_FP_PEAK_RE = re.compile(r"phys_footprint_peak:\s*([\d.]+)\s*(KB|MB|GB)", re.IGNORECASE)
_UNIT_MULT = {"KB": 1.0, "MB": 1024.0, "GB": 1024.0 * 1024.0}


def _to_kb(value: float, unit: str) -> int:
    return int(round(value * _UNIT_MULT[unit.upper()]))


def parse_ps_row(text: str) -> dict:
    """Parse `ps -o rss= -o vsz= -p PID` into {rss_kb, vsz_kb}."""
    m = _PS_RE.search(text or "")
    if not m:
        raise ValueError("unparseable ps row: %r" % (text,))
    return {"rss_kb": int(m.group(1)), "vsz_kb": int(m.group(2))}


def parse_footprint(text: str) -> dict:
    """Parse `footprint -p PID` into {footprint_kb, physical_kb, peak_kb}."""
    head = _FP_HEAD_RE.search(text or "")
    phys = _FP_PHYS_RE.search(text or "")
    peak = _FP_PEAK_RE.search(text or "")
    return {
        "footprint_kb": _to_kb(float(head.group(1)), head.group(2)) if head else None,
        "physical_kb": _to_kb(float(phys.group(1)), phys.group(2)) if phys else None,
        "peak_kb": _to_kb(float(peak.group(1)), peak.group(2)) if peak else None,
    }


def sample_rss(pid: int) -> dict:
    """Raw RSS + VSZ for a single pid via `ps`."""
    out = subprocess.run(
        ["ps", "-o", "rss=", "-o", "vsz=", "-p", str(int(pid))],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError("ps failed (rc=%s): %s" % (out.returncode, out.stderr.strip()))
    return parse_ps_row(out.stdout)


def sample_footprint(pid: int) -> dict:
    """macOS 'physical footprint' (compressed + shared aware) for a single pid.

    Returns None when footprint is unavailable (e.g. permission), so the probe
    degrades to RSS rather than failing the whole soak.
    """
    try:
        out = subprocess.run(
            ["footprint", "-p", str(int(pid))],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    try:
        return parse_footprint(out.stdout)
    except Exception:
        return None


def sample_memory(pid: int) -> dict:
    """The memory snapshot for the probe's OWN server pid.

    `rss_kb`/`vsz_kb` come from `ps`; `physical_kb`/`footprint_kb`/`peak_kb` come
    from `footprint` (the canonical macOS physical footprint). Physical footprint
    is the headline metric because it counts compressed + shared pages; RSS alone
    over-reports under memory pressure. `source` records which metrics are real.
    """
    rss = sample_rss(pid)
    fp = sample_footprint(pid)
    mem = {
        "rss_kb": rss["rss_kb"],
        "vsz_kb": rss["vsz_kb"],
        "footprint_kb": (fp or {}).get("footprint_kb"),
        "physical_kb": (fp or {}).get("physical_kb"),
        "peak_kb": (fp or {}).get("peak_kb"),
        "source": "footprint+rss" if fp else "rss_only",
    }
    return mem


# ---------------------------------------------------------------------------
# Slope + checkpoint bookkeeping (pure)
# ---------------------------------------------------------------------------

def slope_kb_per_request(prev_mem, curr_mem, prev_count, curr_count):
    """KB-per-request between two checkpoints for both physical and RSS.

    Returns None (not 0) when a segment is degenerate (same count, or one of the
    pair lacks the metric). Never returns a leak verdict — only the slope.
    """
    dc = curr_count - prev_count
    if dc <= 0:
        return None
    def seg(key):
        a, b = prev_mem.get(key), curr_mem.get(key)
        if a is None or b is None:
            return None
        return (b - a) / dc
    return {
        "physical_kb_per_req": seg("physical_kb"),
        "rss_kb_per_req": seg("rss_kb"),
        "requests": dc,
    }


def build_request_plan(total_requests, warmup, checkpoints):
    """Return the list of checkpoint request-counts (sorted, deduped, <= total).

    Baseline (after warmup, before measured requests) is always sampled; these
    are the ADDITIONAL measured-request counts at which to sample again. A
    checkpoint equal to the last measured request is allowed (a normal final one).
    """
    cps = sorted({int(c) for c in checkpoints})
    cps = [c for c in cps if 1 <= c <= total_requests]
    return cps


def plan_phases(warmup, total_requests, checkpoints):
    """Return (warmup_indices, measured_indices, checkpoint_counts).

    measured_indices are 1..total_requests; checkpoint_counts are the subset of
    those at which memory is sampled (the baseline is sampled separately before
    the measured phase begins).
    """
    warmup_indices = list(range(1, int(warmup) + 1)) if warmup > 0 else []
    measured_indices = list(range(1, int(total_requests) + 1))
    cps = build_request_plan(total_requests, warmup, checkpoints)
    return warmup_indices, measured_indices, cps


# ---------------------------------------------------------------------------
# HTTP for the isolated server (stdlib http.client, keep-alive)
# ---------------------------------------------------------------------------

def decode_error(body: bytes) -> str:
    """Best-effort string from a non-200 body (OpenAI error JSON or raw)."""
    if not body:
        return "(empty body)"
    text = body.decode("utf-8", "replace").strip()
    try:
        obj = json.loads(text)
        err = obj.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            typ = err.get("type")
            return "type=%s message=%s" % (typ, msg) if typ else str(msg)
        if isinstance(err, str):
            return err
        return text
    except Exception:
        return text[:300]


def http_post_speech(conn, port, text, voice, fmt, timeout):
    """One POST /v1/audio/speech. Returns a request-record dict.

    `conn` is a kept-alive http.client.HTTPConnection reused across requests
    (mirrors a hot client and avoids per-request socket churn). On a broken
    connection the caller may recreate and retry by calling with conn=None.
    """
    rec = {
        "ok": False, "timeout": False, "status": None, "error": None,
        "duration_s": None, "wav": None, "latency_s": None,
    }
    payload = build_speech_payload(text, voice, fmt)
    t0 = time.monotonic()
    try:
        if conn is None:
            conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
        conn.request("POST", "/v1/audio/speech", body=payload,
                     headers={"Content-Type": "application/json",
                              "Accept": "audio/wav"})
        resp = conn.getresponse()
        body = resp.read()
        rec["latency_s"] = time.monotonic() - t0
        rec["status"] = resp.status
        if resp.status == 200:
            try:
                wav = validate_wav_bytes(body)
                rec["ok"] = True
                rec["duration_s"] = wav["duration_s"]
                rec["wav"] = {"channels": wav["channels"], "rate": wav["rate"],
                              "bits": wav["bits"], "sample_count": wav["sample_count"]}
            except Exception as exc:  # invalid audio -> reject, not half-trust
                rec["error"] = "audio_invalid: %s" % exc
                rec["duration_s"] = None
        else:
            rec["error"] = "http_%s: %s" % (resp.status, decode_error(body))
        return rec
    except socket.timeout:
        rec["timeout"] = True
        rec["error"] = "timeout after %ss" % timeout
        return rec
    except (http.client.HTTPException, OSError, ConnectionError) as exc:
        rec["error"] = "connection: %s" % exc
        return rec


# ---------------------------------------------------------------------------
# Loopback listener ownership verification (safety gate before any HTTP)
# ---------------------------------------------------------------------------
# The /health string alone is NOT sufficient proof we own the socket: a bind
# race could let another process take the port. We require, via lsof, that the
# single LISTEN socket on OUR port is (a) bound to loopback (127.0.0.1, not
# `*`/all-interface) and (b) owned by OUR spawned pid. Only then do we talk to
# it. A `*` bind or a foreign owner is a hard blocker — never a caveat.
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def parse_lsof_listeners(text, port) -> list:
    """Parse `lsof -nP -Fnp -sTCP:LISTEN` output -> [{pid, host}].

    Structured field output is unambiguous (no column order to trust):
      * `p<pid>`   starts a process record;
      * `f<fd>`    file descriptor (ignored);
      * `n<h:p>`   the name as `host:port` (numeric via -P; e.g. `127.0.0.1:8081`
                   or `*:8081`) — no `TCP`/`(LISTEN)` decoration in -F form.
    We split the `n` value on the LAST `:` to separate host from port (IPv4 and
    IPv6-safe). Returns [] when nothing is listening (lsof rc=1, empty stdout).
    """
    out = []
    if not text:
        return out
    want = str(int(port))
    cur_pid = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        code = line[0]
        val = line[1:]
        if code == "p":
            try:
                cur_pid = int(val)
            except ValueError:
                cur_pid = None
        elif code == "n" and cur_pid is not None:
            host, _, pport = val.rpartition(":")
            if pport == want:
                out.append({"pid": cur_pid, "host": host.strip("[]")})
    return out


def confirm_loopback_owner(port, own_pid) -> dict:
    """One-shot lsof check: does OUR pid own a loopback-only LISTEN on `port`?"""
    if not shutil.which("lsof"):
        return {"ok": False, "reason": "lsof_unavailable", "required": True,
                "listeners": [], "host": None, "pid": None, "expected_pid": own_pid}
    try:
        out = subprocess.run(["lsof", "-nP", "-Fnp", "-iTCP:%d" % int(port),
                              "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=30)
    except Exception as exc:
        return {"ok": False, "reason": "lsof_error: %s" % exc, "required": True,
                "listeners": [], "host": None, "pid": None, "expected_pid": own_pid}
    # lsof rc=1 means "no matches" (normal when nothing is on the port).
    listeners = parse_lsof_listeners(out.stdout, port)
    if not listeners:
        return {"ok": False, "reason": "no_listener", "required": True,
                "listeners": [], "host": None, "pid": None, "expected_pid": own_pid}
    if len(listeners) > 1:
        return {"ok": False, "reason": "multiple_listeners", "required": True,
                "listeners": listeners, "host": None, "pid": None,
                "expected_pid": own_pid}
    only = listeners[0]
    if only["pid"] != own_pid:
        return {"ok": False, "reason": "foreign_pid", "required": True,
                "listeners": listeners, "host": only["host"], "pid": only["pid"],
                "expected_pid": own_pid}
    if only["host"] not in LOOPBACK_HOSTS:
        return {"ok": False, "reason": "not_loopback", "required": True,
                "listeners": listeners, "host": only["host"], "pid": only["pid"],
                "expected_pid": own_pid}
    return {"ok": True, "reason": None, "required": True, "listeners": listeners,
            "host": only["host"], "pid": only["pid"], "expected_pid": own_pid}


def wait_for_safe_listener(port, proc, own_pid, start_timeout, poll):
    """Poll until OUR pid owns a loopback-only LISTEN on `port`.

    Returns (safe, record, elapsed_s). `safe` is True only when the owning pid is
    ours and the bind is loopback. Definitive failures (foreign pid, not
    loopback, multiple listeners, no lsof) return immediately; `no_listener`
    keeps polling (the server binds only after model load + warmup). A process
    exit is a definitive failure.
    """
    deadline = time.monotonic() + float(start_timeout)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False, {"reason": "process_exited", "returncode": proc.poll(),
                           "required": True}, time.monotonic()
        rec = confirm_loopback_owner(port, own_pid)
        if rec["ok"]:
            return True, rec, time.monotonic()
        if rec["reason"] in ("lsof_unavailable", "lsof_error", "foreign_pid",
                              "not_loopback", "multiple_listeners"):
            return False, rec, time.monotonic()
        # no_listener: not bound yet -> keep polling.
        time.sleep(float(poll))
    return False, {"reason": "timeout", "required": True}, time.monotonic()


# ---------------------------------------------------------------------------
# Readiness polling (the binary has no readiness flag; we implement it)
# ---------------------------------------------------------------------------

def wait_ready(port, proc, start_timeout, poll):
    """Poll GET /health until 200. Returns (ready, elapsed_s).

    Call only after `wait_for_safe_listener` confirms a loopback listener owned
    by our pid; this is the final application-health check (the server binds its
    socket only after model load + warmup).
    """
    deadline = time.monotonic() + float(start_timeout)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False, time.monotonic()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=2.0)
            conn.request("GET", "/health")
            resp = conn.getresponse()
            body = resp.read()
            if resp.status == 200:
                return True, time.monotonic()
            conn.close()
        except Exception:
            pass
        time.sleep(float(poll))
    return False, time.monotonic()


# ---------------------------------------------------------------------------
# Graceful teardown of OUR OWN pid only
# ---------------------------------------------------------------------------

def terminate_owned(proc, grace=DEFAULT_TERM_GRACE):
    """SIGTERM then bounded-wait then SIGKILL, ONLY for this proc's pid.

    Never uses pkill, never kills a process group, never signals by name. The
    server installs a SIGTERM handler that sets a stop flag and closes its
    listen socket, so SIGTERM is the graceful path.
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()  # SIGTERM
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        try:
            proc.kill()   # SIGKILL, same process only
            proc.wait(timeout=grace)
        except Exception:
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def assemble_report(cfg, binary, models_dir, tokenizer, source_voices,
                    voices_temp, port, listener, mem_samples, req_records,
                    checkpoints, wall_secs, complete, prereq_problems=None):
    home = str(Path.home())
    # Build slope segments from the ordered checkpoint samples.
    segments = []
    for a, b in zip(mem_samples, mem_samples[1:]):
        seg = slope_kb_per_request(a["mem"], b["mem"], a["count"], b["count"])
        if seg is not None:
            segments.append({
                "from_label": a["label"], "to_label": b["label"],
                "from_count": a["count"], "to_count": b["count"],
                **seg,
            })
    ok_reqs = [r for r in req_records if r.get("ok")]
    return {
        "schema": "local-tts-service-soak",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "binary": {
            "path": normalize_path(binary, home),
            "sha256": sha256_file(binary),
            "size_bytes": binary.stat().st_size,
        },
        "isolation": {
            "port": port,
            "bind_host_expected": "127.0.0.1",
            "bind_loopback_required": True,
            "listener_confirmed": listener,
            "prod_port_never_touched": 8081,
            "models_dir": normalize_path(models_dir, home),
            "tokenizer": normalize_path(tokenizer, home),
            "source_voices": normalize_path(source_voices, home),
            "temp_voices_dir": normalize_path(voices_temp, home),
            "voice_caches_in_temp": True,
            "deviation_flags_passed": [],
        },
        "config": {
            "base_phrase": cfg.text,
            "text_mode": cfg.text_mode,
            "text_strategy": {
                "mode": cfg.text_mode,
                "warmup": "fixed base phrase",
                "measured": ("base phrase + deterministic request number "
                              "(fresh AR generation each request)" if cfg.text_mode == "varied"
                              else "same base phrase every request (cache-key control)"),
                "note": "varied text keeps output length ~constant (short template) so "
                        "the run stays bounded; audio duration/latency are NOT "
                        "cross-comparable across differing output lengths (trend only).",
            },
            "voice": cfg.voice,
            "warmup_requests": cfg.warmup,
            "total_measured_requests": cfg.requests,
            "checkpoints": checkpoints,
            "max_seconds": cfg.max_seconds,
            "request_timeout_secs": cfg.request_timeout,
            "start_timeout_secs": cfg.start_timeout,
            "ready_poll_secs": cfg.ready_poll,
            "term_grace_secs": cfg.term_grace,
            "keep_alive": cfg.keep_alive,
            "server_timeout_secs": cfg.http_timeout,
            "output_dir": normalize_path(Path(cfg.output_dir), home),
        },
        "server_cmd": {
            "argv": [normalize_text(c, home) for c in cfg._server_cmd],
            "server_flags": SERVER_FLAGS,
            "forbidden_flags_present": [f for f in cfg._server_cmd if any(
                f.startswith(p) for p in FORBIDDEN_FLAG_PREFIXES)],
        },
        "complete": complete,
        "memory_samples": mem_samples,
        "memory_slope_segments": segments,
        "requests": {
            "total": len(req_records),
            "ok": len(ok_reqs),
            "failed": len(req_records) - len(ok_reqs),
            "timeouts": sum(1 for r in req_records if r.get("timeout")),
            "http_errors": [r for r in req_records if not r.get("ok") and not r.get("timeout")],
            "timing_caveat": "per-request audio length varies with text; duration/latency "
                             "medians are trend information only, NOT a per-second fairness "
                             "comparison (the memory probe is not a throughput benchmark).",
            "duration_s_median": _median([r.get("duration_s") for r in ok_reqs]),
            "latency_s_median": _median([r.get("latency_s") for r in ok_reqs]),
        },
        "wall_secs": wall_secs,
        "note": (
            "Memory slope is recorded as evidence, NOT a leak verdict. Early "
            "growth between baseline and the first checkpoint is the voice cache "
            "(.emb/.kv) plus first-render warm and is expected; a plateau "
            "afterwards is the healthy signal. The measured phase uses a "
            "deterministic varied text (base phrase + request number) so the probe "
            "exercises fresh AR generation rather than replaying one cached key — "
            "this is a footprint trend, not a guarantee of no leaks. A sustained "
            "positive slope across every segment is the thing to investigate, "
            "ideally against a no-request idle control."
        ),
    }


def _median(vals):
    import statistics
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return round(statistics.median(vals), 3)


# ---------------------------------------------------------------------------
# Pre-flight validation
# ---------------------------------------------------------------------------

def validate_prereqs(binary, models_dir, tokenizer, source_voices, voice,
                     quiet=False):
    problems = []
    if not binary.exists() or not os.access(binary, os.X_OK):
        problems.append(("error", "installed binary not found/executable: %s" % binary))
    if not models_dir.is_dir():
        problems.append(("error", "models dir missing: %s" % models_dir))
    if not tokenizer.exists():
        problems.append(("error", "tokenizer missing: %s" % tokenizer))
    if not source_voices.is_dir():
        problems.append(("error", "source voices dir missing: %s" % source_voices))
    elif not (source_voices / voice).exists():
        problems.append(("error", "voice file missing: %s" % (source_voices / voice)))
    if not shutil.which("footprint"):
        problems.append(("warning", "'footprint' not on PATH; probe degrades to RSS-only"))
    if not shutil.which("lsof"):
        problems.append(("error", "'lsof' not on PATH; required to verify the loopback "
                                "listener is owned by the spawned pid"))
    return problems


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Repeated-use memory probe for the installed local-tts HTTP server.")
    p.add_argument("--binary", default=str(DEFAULT_BINARY),
                   help="installed pocket-tts binary (default %s)" % DEFAULT_BINARY)
    p.add_argument("--models-dir", default=str(DEFAULT_MODELS),
                   help="production (read-only) model dir (default %s)" % DEFAULT_MODELS)
    p.add_argument("--tokenizer", default=None,
                   help="tokenizer path (default <models-dir>/tokenizer.model)")
    p.add_argument("--voices-dir", default=str(DEFAULT_VOICES),
                   help="SOURCE voices dir holding the stock voice (default %s)" % DEFAULT_VOICES)
    p.add_argument("--voice", default=DEFAULT_VOICE,
                   help="stock voice filename in source dir (default %s)" % DEFAULT_VOICE)
    p.add_argument("--port", type=int, default=None,
                   help="server port (default: auto-pick a free ephemeral port)")
    p.add_argument("--output-dir", default=None,
                   help="output dir (default a fresh temp dir under the OS temp dir)")
    p.add_argument("--json-out", default=None, help="path to write the JSON report")
    p.add_argument("--text", default=DEFAULT_PHRASE,
                   help="one fixed synthetic phrase (default: the built-in phrase)")
    p.add_argument("--text-mode", default="varied", choices=["fixed", "varied"],
                   help="measured-phase text strategy: varied (base + request number, "
                        "default) exercises fresh AR generation; fixed uses the same "
                        "text every request (cache-key control). Warmup always uses the "
                        "base phrase.")
    p.add_argument("--requests", type=int, default=DEFAULT_REQUESTS,
                   help="total measured requests after warmup (default %d)" % DEFAULT_REQUESTS)
    p.add_argument("--warmup", type=int, default=DEFAULT_WARMUP,
                   help="discarded warmup requests before baseline (default %d)" % DEFAULT_WARMUP)
    p.add_argument("--checkpoints", default=",".join(map(str, DEFAULT_CHECKPOINTS)),
                   help="comma-separated measured-request counts to sample memory "
                        "(default %s)" % ",".join(map(str, DEFAULT_CHECKPOINTS)))
    p.add_argument("--min-ok-samples", type=int, default=0,
                   help="minimum valid requests required for exit 0 (0 = skip check)")
    p.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS,
                   help="hard wall-clock ceiling in seconds (default %d)" % DEFAULT_MAX_SECONDS)
    p.add_argument("--request-timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT,
                   help="per-request HTTP timeout in seconds (default %.0f)" % DEFAULT_REQUEST_TIMEOUT)
    p.add_argument("--http-timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT,
                   help="alias for --request-timeout (default %.0f)" % DEFAULT_REQUEST_TIMEOUT)
    p.add_argument("--start-timeout", type=float, default=DEFAULT_START_TIMEOUT,
                   help="must answer /health within this many seconds (default %.0f)"
                        % DEFAULT_START_TIMEOUT)
    p.add_argument("--ready-poll", type=float, default=DEFAULT_READY_POLL,
                   help="/health poll interval in seconds (default %.2f)" % DEFAULT_READY_POLL)
    p.add_argument("--term-grace", type=float, default=DEFAULT_TERM_GRACE,
                   help="SIGTERM->SIGKILL grace for our own pid (default %.0f)" % DEFAULT_TERM_GRACE)
    p.add_argument("--no-keep-alive", action="store_true",
                   help="open a fresh connection per request (default: keep-alive)")
    p.add_argument("--verbose", action="store_true",
                   help="echo each request + sample")
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan and server command without executing")
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_soak(cfg):
    home = str(Path.home())
    binary = Path(cfg.binary).expanduser()
    models_dir = Path(cfg.models_dir).expanduser()
    source_voices = Path(cfg.voices_dir).expanduser()
    tokenizer = Path(cfg.tokenizer).expanduser() if cfg.tokenizer else models_dir / "tokenizer.model"
    voice = cfg.voice

    prereqs = validate_prereqs(binary, models_dir, tokenizer, source_voices, voice)
    errors = [m for k, m in prereqs if k == "error"]
    for kind, msg in prereqs:
        print("[%s] %s" % (kind.upper(), msg), file=sys.stderr)
    if errors:
        print("FAIL: pre-flight validation failed.", file=sys.stderr)
        return {"_exit": 1, "prereq_problems": prereqs}

    warmup_indices, measured_indices, checkpoints = plan_phases(
        cfg.warmup, cfg.requests, [c for c in (cfg.checkpoints.split(",") if isinstance(
            cfg.checkpoints, str) else cfg.checkpoints) if c])
    checkpoints = [int(c) for c in checkpoints]

    if cfg.dry_run:
        # PURELY print the plan: no temp dirs, no socket bind, no FS writes,
        # no server spawn, no HTTP. The parent reviews this before the real run.
        print("DRY RUN (no execution; no server spawned).")
        print("binary:   %s" % normalize_path(binary, home))
        print("models:   %s (read-only)" % normalize_path(models_dir, home))
        print("tokenizer:%s" % normalize_path(tokenizer, home))
        print("voice src:%s  voice: %s" % (normalize_path(source_voices, home), voice))
        display_voices = normalize_path(
            Path(cfg.output_dir or "/tmp/local-tts-soak") / "voices", home)
        print("temp voices: %s  (fresh each run; caches land here, not production)"
              % display_voices)
        cmd_display = build_server_command(
            binary, cfg.port if cfg.port else 0, models_dir, tokenizer,
            Path("/tmp/local-tts-soak/voices"), extra_flags=())
        print("port:%s  requests:%d  warmup:%d  checkpoints:%s"
              % (cfg.port if cfg.port else "<auto ephemeral>", cfg.requests,
                 cfg.warmup, checkpoints))
        print("text-mode:%s  (varied = base phrase + request number; fixed = same text)"
              % cfg.text_mode)
        print("max_seconds:%s  request_timeout:%s  start_timeout:%s"
              % (cfg.max_seconds, cfg.request_timeout, cfg.start_timeout))
        print("\nserver command (isolated; same production flags except port + paths):")
        print("  %s" % " ".join(normalize_text(c, home) for c in cmd_display))
        print("listener safety gate: require this pid to own a LOOPBACK-only (127.0.0.1)"
              " LISTEN on the port (lsof), BEFORE any request; /health alone is not enough.")
        print("installed-binary note: the pre-fix installed binary binds all interfaces,"
              " so the loopback gate rejects it; pass --binary <freshly-built-loopback binary>.")
        return {"_exit": 0, "dry_run": True, "checkpoints": checkpoints,
                "server_cmd": cmd_display}

    output_dir = Path(cfg.output_dir or tempfile.mkdtemp(prefix="local-tts-soak-"))
    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir = output_dir / "runs"
    log_dir.mkdir(exist_ok=True)

    # Isolate: fresh temp voices copy (voice caches land here, never production).
    voices_temp, dest_voice = make_temp_voices_dir(source_voices / voice,
                                                   root=output_dir)

    port = cfg.port if cfg.port else pick_free_port()
    server_cmd = build_server_command(binary, port, models_dir, tokenizer,
                                      voices_temp, extra_flags=())
    cfg._server_cmd = server_cmd

    # Isolation guard: the built command must NOT deviate from production flags
    # and must NOT point at the production voices dir.
    forbidden = forbidden_flags(server_cmd)
    if forbidden:
        print("FAIL: server command contains forbidden production-deviation flags: %s"
              % forbidden, file=sys.stderr)
        return {"_exit": 1, "forbidden_flags": forbidden}

    # ---- spawn the isolated server ----
    log_path = log_dir / ("server-%s.log" % time.strftime("%H%M%S"))
    log_fh = open(log_path, "w", buffering=1)
    try:
        proc = subprocess.Popen(
            server_cmd,
            stdout=log_fh, stderr=subprocess.STDOUT,
            cwd=str(binary.parent), start_new_session=False,
        )
    except Exception as exc:
        log_fh.close()
        print("FAIL: could not spawn server: %s" % exc, file=sys.stderr)
        return {"_exit": 1, "spawn_error": str(exc)}

    wall_start = time.monotonic()
    mem_samples = []
    req_records = []
    complete = False
    try:
        # Safety gate: OUR pid must own a loopback-only listener BEFORE any HTTP.
        # A bind race (foreign pid) or a non-loopback (`*`) listener is a hard
        # blocker, never a caveat.
        safe, listener, _ = wait_for_safe_listener(port, proc, proc.pid,
                                                   cfg.start_timeout, cfg.ready_poll)
        if not safe:
            print("FAIL: unsafe listener on port %s (reason=%s): %s"
                  % (port, listener.get("reason"), listener), file=sys.stderr)
            tr = {"reason": listener.get("reason"), "listener": listener,
                  "log": normalize_path(log_path, home)}
            return {"_exit": 2, "ready": False, "listener": tr, "log": normalize_path(log_path, home)}

        # Application-health check (after ownership is confirmed).
        ready, elapsed = wait_ready(port, proc, cfg.start_timeout, cfg.ready_poll)
        if not ready:
            print("FAIL: server not ready within %.1fs (elapsed %.1fs); proc rc=%s"
                  % (cfg.start_timeout, elapsed, proc.poll()), file=sys.stderr)
            return {"_exit": 2, "ready": False, "log": normalize_path(log_path, home)}

        # Warmup requests (build the voice cache / first-render) — discarded.
        conn = http.client.HTTPConnection("127.0.0.1", int(port),
                                          timeout=cfg.request_timeout)
        for i in warmup_indices:
            if time.monotonic() - wall_start > cfg.max_seconds:
                break
            rec = http_post_speech(conn, port, cfg.text, voice, "wav",
                                   cfg.request_timeout)
            if cfg.verbose:
                print("WARMUP %d  ok=%s  dur=%s  %s" % (i, rec["ok"], rec["duration_s"],
                                                        rec.get("error") or ""),
                      file=sys.stderr)
            if not rec["ok"] and rec.get("error") and "connection" in rec["error"]:
                # keep-alive dropped; recreate the connection and retry once.
                conn.close()
                conn = http.client.HTTPConnection("127.0.0.1", int(port),
                                                  timeout=cfg.request_timeout)
                rec = http_post_speech(conn, port, cfg.text, voice, "wav",
                                       cfg.request_timeout)

        # Baseline sample (after warmup, before the measured phase).
        mem_samples.append({
            "label": "baseline", "count": 0, "wall_s": round(time.monotonic() - wall_start, 3),
            "mem": sample_memory(proc.pid),
        })

        # Measured requests; sample memory at each checkpoint count.
        cp_set = set(checkpoints)
        for i in measured_indices:
            if time.monotonic() - wall_start > cfg.max_seconds:
                break
            req_text = text_for_request(i, cfg.text, cfg.text_mode)
            rec = http_post_speech(conn, port, req_text, voice, "wav",
                                   cfg.request_timeout)
            if not rec["ok"] and rec.get("error") and "connection" in rec["error"]:
                conn.close()
                conn = http.client.HTTPConnection("127.0.0.1", int(port),
                                                  timeout=cfg.request_timeout)
                rec = http_post_speech(conn, port, req_text, voice, "wav",
                                       cfg.request_timeout)
            rec["index"] = i
            rec["text"] = req_text
            rec["text_mode"] = cfg.text_mode
            req_records.append(rec)
            if cfg.verbose:
                print("REQ %d  ok=%s  dur=%s  %s" % (i, rec["ok"], rec["duration_s"],
                                                     rec.get("error") or ""),
                      file=sys.stderr)
            if i in cp_set:
                mem_samples.append({
                    "label": "checkpoint_%d" % i, "count": i,
                    "wall_s": round(time.monotonic() - wall_start, 3),
                    "mem": sample_memory(proc.pid),
                })

        # All requested checkpoints reached?
        reached = {s["count"] for s in mem_samples}
        complete = bool(checkpoints) and all(c in reached for c in checkpoints) \
            and bool(mem_samples)

        if cfg.verbose:
            for s in mem_samples:
                print("  MEM %-14s count=%3d  rss=%sKB  phys=%sKB  peak=%sKB"
                      % (s["label"], s["count"], s["mem"]["rss_kb"],
                         s["mem"].get("physical_kb"), s["mem"].get("peak_kb")),
                      file=sys.stderr)

        # ---- graceful teardown of OUR OWN pid ----
        try:
            conn.close()
        except Exception:
            pass
        terminate_owned(proc, cfg.term_grace)

        wall_secs = time.monotonic() - wall_start

        ok_reqs = [r for r in req_records if r.get("ok")]
        if cfg.min_ok_samples and len(ok_reqs) < cfg.min_ok_samples:
            complete = False

        report = assemble_report(cfg, binary, models_dir, tokenizer, source_voices,
                                 voices_temp, port, listener, mem_samples, req_records,
                                 checkpoints, wall_secs, complete,
                                 prereq_problems=prereqs)

        json_path = cfg.json_out if cfg.json_out else output_dir / (
            "soak-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
        json_path = Path(json_path)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(report, indent=2))
        print("Report: %s" % normalize_path(json_path, home))

        # Console summary.
        print("\n===== SOAK MEMORY SUMMARY (physical footprint, KB) =====")
        for s in mem_samples:
            print("  %-16s count=%3d  rss=%8s  phys=%8s  peak=%8s  wall=%6.1fs"
                  % (s["label"], s["count"], s["mem"]["rss_kb"],
                     s["mem"].get("physical_kb") or "-",
                     s["mem"].get("peak_kb") or "-", s["wall_s"]))
        print("\nmemory slope (KB/request) between checkpoints:")
        for seg in report["memory_slope_segments"]:
            print("  %-14s -> %-14s  phys=%s  rss=%s"
                  % (seg["from_label"], seg["to_label"],
                     seg["physical_kb_per_req"] if seg["physical_kb_per_req"] is not None else "-",
                     seg["rss_kb_per_req"] if seg["rss_kb_per_req"] is not None else "-"))

        if not complete:
            print("\nWARNING: soak incomplete; some checkpoint(s) not reached or too few "
                  "valid requests.", file=sys.stderr)

        return report
    finally:
        # Regardless of how we leave the try block, make sure OUR OWN server is
        # gone. This is idempotent: terminate_owned checks poll() first.
        terminate_owned(proc, cfg.term_grace)
        try:
            log_fh.close()
        except Exception:
            pass


def main(argv=None):
    cfg = parse_args(argv if argv is not None else sys.argv[1:])
    cfg.keep_alive = not cfg.no_keep_alive
    report = run_soak(cfg)
    if report.get("_exit") is not None:
        return report["_exit"]
    if report.get("dry_run"):
        return 0
    return 0 if report.get("complete") else 2


if __name__ == "__main__":
    sys.exit(main())
