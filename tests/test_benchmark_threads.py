#!/usr/bin/env python3
"""Tests for tools/benchmark_threads.py (parser + aggregation helpers).

Pure stdlib unittest; does NOT run the pocket-tts binary. Run directly:

    python3 tests/test_benchmark_threads.py
"""

import math
import struct
import subprocess
import sys
import unittest
from array import array
from pathlib import Path

# Make the tools package importable without an __init__.py.
_TOOLS = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import benchmark_threads as bt  # noqa: E402


def build_wav(path, values, rate=24000, channels=1, fmt=3, bits=32):
    """Write a minimal RIFF/WAVE float32 file."""
    bytes_per = bits // 8
    byte_rate = rate * channels * bytes_per
    block_align = channels * bytes_per
    fmt_payload = struct.pack("<HHIIHH", fmt, channels, rate, byte_rate, block_align, bits)
    body = b"fmt " + struct.pack("<I", len(fmt_payload)) + fmt_payload
    arr = array("f")
    arr.fromlist(list(values))
    data_bytes = arr.tobytes()
    body += b"data" + struct.pack("<I", len(data_bytes)) + data_bytes
    riff = b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body
    path.write_bytes(riff)


class TestParseLog(unittest.TestCase):
    def test_realistic_transcript(self):
        text = (
            'Loading (precision=int8, threads=5)...\n'
            '  Loaded in 0.23s\n'
            'Generating: "hello" with /Users/tristan/Models/pocket-tts/voices/example.wav\n'
            '  First-chunk stages:\n'
            '    stream_start            0.000ms  +0.000ms\n'
            '  3.97s audio in 0.18s (RTFx: 22.45x)\n'
            '  First chunk latency: 27ms\n'
            '  Saved: /tmp/out.wav\n'
        )
        p = bt.parse_log(text)
        self.assertEqual(p["rtfx"], 22.45)
        self.assertEqual(p["load_secs"], 0.23)
        self.assertEqual(p["first_chunk_ms"], 27.0)
        self.assertEqual(p["audio_duration_s"], 3.97)
        self.assertEqual(p["gen_secs"], 0.18)
        self.assertTrue(bt.is_usable_metrics(p))

    def test_rtfx_different_precision(self):
        text = "  0.50s audio in 1.00s (RTFx: 0.50x)\n"
        self.assertEqual(bt.parse_log(text)["rtfx"], 0.50)

    def test_garbage_returns_none(self):
        p = bt.parse_log("no metrics here\n")
        self.assertIsNone(p["rtfx"])
        self.assertIsNone(p["load_secs"])
        self.assertIsNone(p["first_chunk_ms"])
        self.assertFalse(bt.is_usable_metrics(p))

    def test_missing_first_chunk_not_usable(self):
        # profile off => no "First chunk latency" line
        text = "  Loaded in 0.23s\n  3.97s audio in 0.18s (RTFx: 22.45x)\n"
        p = bt.parse_log(text)
        self.assertIsNotNone(p["rtfx"])
        self.assertIsNotNone(p["load_secs"])
        self.assertIsNone(p["first_chunk_ms"])
        self.assertFalse(bt.is_usable_metrics(p))


class TestParseWav(unittest.TestCase):
    def test_valid_float32_mono_24k(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            p = Path(f.name)
        try:
            build_wav(p, [0.1, -0.2, 0.3, 0.5])
            w = bt.parse_wav(p)
            self.assertEqual(w["format"], 3)
            self.assertEqual(w["channels"], 1)
            self.assertEqual(w["rate"], 24000)
            self.assertEqual(w["bits"], 32)
            self.assertEqual(w["sample_count"], 4)
            self.assertAlmostEqual(w["duration_s"], 4 / 24000)
        finally:
            p.unlink(missing_ok=True)

    def test_nan_rejected(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            p = Path(f.name)
        try:
            build_wav(p, [0.1, float("nan")])
            with self.assertRaises(ValueError):
                bt.parse_wav(p)
        finally:
            p.unlink(missing_ok=True)

    def test_empty_rejected(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            p = Path(f.name)
        try:
            build_wav(p, [])
            with self.assertRaises(ValueError):
                bt.parse_wav(p)
        finally:
            p.unlink(missing_ok=True)

    def test_wrong_sample_rate_rejected(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            p = Path(f.name)
        try:
            build_wav(p, [0.1, 0.2], rate=48000)
            with self.assertRaises(ValueError):
                bt.parse_wav(p)
        finally:
            p.unlink(missing_ok=True)

    def test_not_float_rejected(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            p = Path(f.name)
        try:
            build_wav(p, [0.1, 0.2], fmt=1, bits=16)  # PCM s16
            with self.assertRaises(ValueError):
                bt.parse_wav(p)
        finally:
            p.unlink(missing_ok=True)

    def test_not_wave_rejected(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            p = Path(f.name)
        try:
            p.write_bytes(b"NOTAWAVFILE........")
            with self.assertRaises(ValueError):
                bt.parse_wav(p)
        finally:
            p.unlink(missing_ok=True)


class TestAggregate(unittest.TestCase):
    def _run(self, config, kind, index, rtfx, fcm=10.0, load=0.2, wall=0.5, ok=True):
        return {
            "config": config, "kind": kind, "index": index,
            "rtfx": rtfx, "first_chunk_ms": fcm, "load_secs": load, "wall_secs": wall,
            "ok": ok,
        }

    def test_median_and_range(self):
        runs = [
            self._run("auto", "sample", 0, 10),
            self._run("auto", "sample", 1, 20),
            self._run("auto", "sample", 2, 30),
        ]
        res, complete = bt.aggregate(runs, required_samples=3)
        self.assertTrue(complete)
        m = res["auto"]["metrics"]["rtfx"]
        self.assertEqual(m["median"], 20)
        self.assertEqual(m["min"], 10)
        self.assertEqual(m["max"], 30)
        self.assertEqual(m["n"], 3)
        self.assertEqual(res["auto"]["samples_reported"], 3)

    def test_incomplete_when_below_required(self):
        runs = [self._run("auto", "sample", 0, 10)]
        res, complete = bt.aggregate(runs, required_samples=7)
        self.assertFalse(complete)
        self.assertFalse(res["auto"]["complete"])

    def test_median_of_even_count_is_average_of_middle(self):
        runs = [self._run("a", "sample", 0, 2), self._run("a", "sample", 1, 4)]
        res, _ = bt.aggregate(runs, required_samples=2)
        self.assertEqual(res["a"]["metrics"]["rtfx"]["median"], 3.0)

    def test_missing_config_is_reported_incomplete(self):
        # auto has 7 valid runs, 2/2/4 has none; the benchmark must be flagged
        # incomplete, and 2/2/4 must still appear (not vanish) in the results.
        runs = [self._run("auto", "sample", i, 10.0) for i in range(7)]
        res, complete = bt.aggregate(runs, required_samples=7, configs=["auto", "2/2/4"])
        self.assertFalse(complete)
        self.assertIn("auto", res)
        self.assertIn("2/2/4", res)
        self.assertTrue(res["auto"]["complete"])
        self.assertFalse(res["2/2/4"]["complete"])
        self.assertEqual(res["2/2/4"]["samples_reported"], 0)

    def test_keys_present_for_all_metrics(self):
        runs = [self._run("a", "sample", 0, 10, fcm=12.0, load=0.3, wall=0.7)]
        res, _ = bt.aggregate(runs, required_samples=1)
        self.assertIn("first_chunk_ms", res["a"]["metrics"])
        self.assertIn("load_secs", res["a"]["metrics"])
        self.assertIn("wall_secs", res["a"]["metrics"])


class TestFlagsAndCommand(unittest.TestCase):
    def test_auto_spec_no_flags(self):
        self.assertEqual(bt.thread_flags_for_spec(None), [])

    def test_explicit_spec_flags(self):
        self.assertEqual(bt.thread_flags_for_spec((2, 2, 4)),
                         ["--threads-ar", "2", "--threads-dec", "2", "--threads-full", "4"])

    def test_build_command_base_and_positionals(self):
        cmd = bt.build_command(
            "/bin/pocket-tts", ["--temperature", "0"], ["--threads-ar", "3"],
            "/m/models", "/m/models/tokenizer.model", "/m/voices",
            "hello world", "example.wav", "/tmp/out.wav")
        self.assertEqual(cmd[:2], ["/bin/pocket-tts", "--temperature"])
        self.assertEqual(cmd[-3:], ["hello world", "example.wav", "/tmp/out.wav"])
        self.assertIn("--temperature", cmd)
        self.assertIn("--threads-ar", cmd)

    def test_build_command_keeps_extra_constant(self):
        cmd = bt.build_command(
            "/bin/pocket-tts", ["--temperature", "0"], [], "/m/models",
            "/m/models/tokenizer.model", "/m/voices", "t", "v.wav", "/tmp/o.wav",
            extra_flags=["--precision", "fp32"])
        self.assertEqual(cmd[1:3], ["--precision", "fp32"])


class TestNormalize(unittest.TestCase):
    def test_home_prefix_becomes_tilde(self):
        home = "/Users/alice"  # pretend
        self.assertEqual(bt.normalize_text("/Users/alice/.local/tts", home), "~/.local/tts")
        self.assertEqual(bt.normalize_text("/Users/alice", home), "/Users/alice")
        self.assertEqual(bt.normalize_text("/Users/bob/.local/tts", home), "/Users/bob/.local/tts")
        self.assertEqual(bt.normalize_text("relative/path", home), "relative/path")


class TestTrialPlan(unittest.TestCase):
    def test_warmup_then_shuffled_measured(self):
        warmup, measured = bt.build_trial_plan(["auto", "2/2/4"], warmup=1, samples=4, seed=42)
        # All warmups are config-order, discarded.
        self.assertEqual([(w["config"], w["kind"]) for w in warmup],
                         [("auto", "warmup"), ("2/2/4", "warmup")])
        # Each config appears exactly once per sample index across the measured set.
        by = {}
        for m in measured:
            by.setdefault(m["config"], set()).add(m["index"])
        self.assertEqual(by["auto"], {0, 1, 2, 3})
        self.assertEqual(by["2/2/4"], {0, 1, 2, 3})
        # Interleaved: no run runs the same config back-to-back for 4+ in a row is not
        # guaranteed, but both configs must be present in the first two measured slots.
        self.assertEqual(len(measured), 8)

    def test_seed_deterministic(self):
        _, a = bt.build_trial_plan(["auto", "2/2/4"], 1, 4, seed=7)
        _, b = bt.build_trial_plan(["auto", "2/2/4"], 1, 4, seed=7)
        self.assertEqual([m["config"] + str(m["index"]) for m in a],
                         [m["config"] + str(m["index"]) for m in b])

    def test_default_phrase_is_single_synthetic(self):
        # Screening uses ONE fixed synthetic phrase (never a mixed set), so no
        # heterogeneous phrase timings are aggregated into a single figure.
        self.assertIsInstance(bt.DEFAULT_PHRASE, str)
        self.assertTrue(bt.DEFAULT_PHRASE.strip())
        self.assertEqual(bt.DEFAULT_PHRASE, "This is a local performance test. Clear speech should stay clear.")


class TestBaseFlags(unittest.TestCase):
    def test_base_flags_defaults_only(self):
        # Only the two diagnostic additions (determinism + profiling). All other
        # flags stay at binary defaults, mirroring the running service, and the
        # voice caches are preserved (no --no-cache, no --low-latency/--trim-*).
        self.assertEqual(bt.BASE_FLAGS, ["--temperature", "0", "--profile"])
        self.assertNotIn("--no-cache", bt.BASE_FLAGS)
        self.assertNotIn("--low-latency", bt.BASE_FLAGS)
        self.assertTrue(
            not any(f.startswith("--trim-") for f in bt.BASE_FLAGS),
            "no --trim-* override should be pinned",
        )


class TestRunTrialTimeout(unittest.TestCase):
    def test_timeout_preserves_partial_output(self):
        # A timed-out run must keep whatever the process wrote before being
        # killed, so failures stay auditable instead of being wiped.
        import tempfile
        prog = "import time; print('PARTIAL_MARKER', flush=True); time.sleep(30)"
        with tempfile.TemporaryDirectory() as tmp:
            params = {"config": "auto", "kind": "sample", "index": 0, "phrase": "x"}
            rec = bt._run_trial(
                params, [sys.executable, "-c", prog],
                str(Path(tmp) / "out.wav"), 0.5, "t0", Path(tmp) / "runs",
            )
            self.assertFalse(rec["ok"])
            self.assertTrue(rec["timeout"])
            self.assertIsNotNone(rec["wall_secs"])
            log = (Path(tmp) / "runs" / "t0.log").read_text()
            self.assertIn("TIMEOUT after 0.5s", log)
            self.assertIn("PARTIAL_MARKER", log)

    def test_bytes_safe_decode_helper(self):
        # TimeoutExpired carries partial output as bytes even with text=True;
        # decoding must be safe against undecodable bytes.
        self.assertEqual(bt._try_decode(b"abc\xffdef"), "abc\ufffddef")
        self.assertEqual(bt._try_decode("plain"), "plain")
        self.assertEqual(bt._try_decode(None), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
