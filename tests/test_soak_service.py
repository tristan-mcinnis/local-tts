#!/usr/bin/env python3
"""Tests for tools/soak_service.py (isolated repeated-use memory probe).

Pure stdlib unittest. These tests mock every external touchpoint (socket, ps,
footprint, http.client, subprocess.Popen) so they NEVER spawn the pocket-tts
server, never make an HTTP request, and never touch the production service or
voices dir. Run directly:

    python3 tests/test_soak_service.py
"""

import array
import json
import socket
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

_TOOLS = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import soak_service as ss  # noqa: E402


def build_wav_bytes(values, rate=24000, channels=1, fmt=3, bits=32):
    """Return a valid float32 RIFF/WAVE byte string."""
    bytes_per = bits // 8
    byte_rate = rate * channels * bytes_per
    block_align = channels * bytes_per
    fmt_payload = struct.pack("<HHIIHH", fmt, channels, rate, byte_rate, block_align, bits)
    body = b"fmt " + struct.pack("<I", len(fmt_payload)) + fmt_payload
    arr = array.array("f", list(values))
    data_bytes = arr.tobytes()
    body += b"data" + struct.pack("<I", len(data_bytes)) + data_bytes
    riff = b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body
    return riff


class TestWavValidation(unittest.TestCase):
    def test_valid_float32_mono_24k(self):
        w = ss.validate_wav_bytes(build_wav_bytes([0.1, -0.2, 0.3, 0.5]))
        self.assertEqual(w["format"], 3)
        self.assertEqual(w["channels"], 1)
        self.assertEqual(w["rate"], 24000)
        self.assertEqual(w["bits"], 32)
        self.assertEqual(w["sample_count"], 4)
        self.assertAlmostEqual(w["duration_s"], 4 / 24000)

    def test_nan_rejected(self):
        with self.assertRaises(ValueError):
            ss.validate_wav_bytes(build_wav_bytes([0.1, float("nan")]))

    def test_inf_rejected(self):
        with self.assertRaises(ValueError):
            ss.validate_wav_bytes(build_wav_bytes([0.1, float("inf")]))

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            ss.validate_wav_bytes(build_wav_bytes([]))

    def test_wrong_rate_rejected(self):
        with self.assertRaises(ValueError):
            ss.validate_wav_bytes(build_wav_bytes([0.1, 0.2], rate=48000))

    def test_not_float_rejected(self):
        with self.assertRaises(ValueError):
            ss.validate_wav_bytes(build_wav_bytes([0.1, 0.2], fmt=1, bits=16))

    def test_not_wave_rejected(self):
        with self.assertRaises(ValueError):
            ss.validate_wav_bytes(b"NOTAWAVFILE........")


class TestServerCommandIsolation(unittest.TestCase):
    def test_command_has_server_port_and_path_overrides(self):
        cmd = ss.build_server_command(
            "/bin/pocket-tts", 39999, "/m/models", "/m/tokenizer.model", "/tmp/voices")
        self.assertEqual(cmd[0], "/bin/pocket-tts")
        self.assertIn("--server", cmd)
        self.assertEqual(cmd[cmd.index("--port") + 1], "39999")
        self.assertEqual(cmd[cmd.index("--models-dir") + 1], "/m/models")
        self.assertEqual(cmd[cmd.index("--tokenizer") + 1], "/m/tokenizer.model")
        self.assertEqual(cmd[cmd.index("--voices-dir") + 1], "/tmp/voices")

    def test_command_has_no_deviation_flags(self):
        cmd = ss.build_server_command(
            "/bin/pocket-tts", 39999, "/m/models", "/m/tokenizer.model", "/tmp/voices")
        self.assertEqual(ss.forbidden_flags(cmd), [])

    def test_forbidden_flags_detector(self):
        # A production-deviation flag must be caught by the isolation guard.
        bad = ss.build_server_command("/bin/p", 1, "/m", "/m/t", "/v",
                                      extra_flags=["--temperature", "0"])
        self.assertEqual(ss.forbidden_flags(bad), ["--temperature"])
        bad2 = ss.build_server_command("/bin/p", 1, "/m", "/m/t", "/v",
                                       extra_flags=["--threads-ar", "2"])
        self.assertEqual(ss.forbidden_flags(bad2), ["--threads-ar"])
        # --port, --voices-dir, --models-dir, --tokenizer, --server are all
        # allowed (they are the four intentional overrides + the mode switch).
        clean = ss.build_server_command("/bin/p", 1, "/m", "/m/t", "/v")
        self.assertEqual(ss.forbidden_flags(clean), [])

    def test_same_production_defaults_no_overrides(self):
        # The whole point is that we pass NO tuning flag: no temperature, no
        # threads, no trim, no cache, no low-latency, no profile.
        cmd = ss.build_server_command(
            "/bin/pocket-tts", 8080, "/m", "/m/t", "/tmp/v")
        self.assertNotIn("--no-cache", cmd)
        self.assertNotIn("--low-latency", cmd)
        self.assertNotIn("--profile", cmd)
        self.assertTrue(not any(a.startswith("--trim-") for a in cmd),
                        "no --trim-* override should be pinned")
        self.assertTrue(not any(a.startswith("--threads") for a in cmd))


class TestPickFreePort(unittest.TestCase):
    def test_returns_assigned_port(self):
        fake = mock.MagicMock()
        fake.return_value.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 42999)
        fake.return_value.__exit__.return_value = False
        with mock.patch.object(ss.socket, "socket", fake):
            self.assertEqual(ss.pick_free_port(), 42999)
        fake.return_value.__enter__.return_value.bind.assert_called_once_with(("127.0.0.1", 0))


class TestMakeTempVoices(unittest.TestCase):
    def test_copies_voice_only_no_caches(self):
        with mock.patch.object(ss.tempfile, "mkdtemp",
                               return_value="/tmp/soak-root") as mkd:
            # We mock Path access so no real FS interaction is required.
            with mock.patch.object(Path, "exists", return_value=True), \
                 mock.patch.object(Path, "mkdir", return_value=None), \
                 mock.patch.object(ss.shutil, "copyfile") as copy:
                voices_dir, voice_path = ss.make_temp_voices_dir(
                    Path("/src/voices/example.wav"))
                self.assertEqual(voice_path.name, "example.wav")
                self.assertEqual(voice_path.parent.name, "voices")
                self.assertTrue(str(voice_path.parent).endswith("voices"))
                copy.assert_called_once_with(Path("/src/voices/example.wav"),
                                             voice_path)

    def test_missing_source_raises(self):
        with mock.patch.object(Path, "exists", return_value=False):
            with self.assertRaises(FileNotFoundError):
                ss.make_temp_voices_dir(Path("/nope/example.wav"))


class TestUrlAndPayload(unittest.TestCase):
    def test_urls(self):
        self.assertEqual(ss.health_url(8081), "http://127.0.0.1:8081/health")
        self.assertEqual(ss.speech_url(8081), "http://127.0.0.1:8081/v1/audio/speech")

    def test_payload(self):
        body = ss.build_speech_payload("hello", "example.wav", "wav")
        obj = json.loads(body)
        self.assertEqual(obj["input"], "hello")
        self.assertEqual(obj["voice"], "example.wav")
        self.assertEqual(obj["response_format"], "wav")
        self.assertIn("model", obj)
        self.assertNotIn("speed", obj)  # accepted+ignored by server; not sent


class TestMemoryParsers(unittest.TestCase):
    def test_parse_ps_row(self):
        m = ss.parse_ps_row("  123456  789012\n")
        self.assertEqual(m["rss_kb"], 123456)
        self.assertEqual(m["vsz_kb"], 789012)

    def test_parse_ps_row_bad(self):
        with self.assertRaises(ValueError):
            ss.parse_ps_row("not a number row")

    def test_parse_footprint(self):
        text = (
            "==============\n"
            "bash [95359]: 64-bit    Footprint: 1216 KB (16384 bytes per page)\n"
            "==============\n"
            "  Total:    1216 KB\n"
            "    phys_footprint: 1232 KB\n"
            "    phys_footprint_peak: 1232 KB\n"
        )
        fp = ss.parse_footprint(text)
        self.assertEqual(fp["footprint_kb"], 1216)
        self.assertEqual(fp["physical_kb"], 1232)
        self.assertEqual(fp["peak_kb"], 1232)

    def test_parse_footprint_units(self):
        text = "  phys_footprint: 1.5 MB\n  phys_footprint_peak: 2 GB\n"
        fp = ss.parse_footprint(text)
        self.assertEqual(fp["physical_kb"], 1536)
        self.assertEqual(fp["peak_kb"], 2 * 1024 * 1024)

    def test_sample_memory_rss_only_fallback(self):
        # footprint fails -> source becomes rss_only, physical_kb None.
        proc = mock.MagicMock()
        proc.returncode = 0
        proc.stdout = "  100  200\n"
        proc.stderr = ""
        with mock.patch.object(ss, "sample_rss", return_value={"rss_kb": 100, "vsz_kb": 200}), \
             mock.patch.object(ss, "sample_footprint", return_value=None):
            m = ss.sample_memory(1234)
            self.assertEqual(m["rss_kb"], 100)
            self.assertEqual(m["physical_kb"], None)
            self.assertEqual(m["source"], "rss_only")

    def test_sample_memory_full(self):
        with mock.patch.object(ss, "sample_rss",
                               return_value={"rss_kb": 100, "vsz_kb": 200}), \
             mock.patch.object(ss, "sample_footprint",
                               return_value={"footprint_kb": 90, "physical_kb": 95,
                                             "peak_kb": 97}):
            m = ss.sample_memory(1234)
            self.assertEqual(m["physical_kb"], 95)
            self.assertEqual(m["source"], "footprint+rss")


class TestSlopeAndPlan(unittest.TestCase):
    def test_slope_physical(self):
        prev = {"physical_kb": 1000, "rss_kb": 1100}
        curr = {"physical_kb": 1100, "rss_kb": 1200}
        seg = ss.slope_kb_per_request(prev, curr, 10, 50)
        self.assertEqual(seg["physical_kb_per_req"], 2.5)   # (1100-1000)/40
        self.assertEqual(seg["rss_kb_per_req"], 2.5)
        self.assertEqual(seg["requests"], 40)

    def test_slope_degenerate(self):
        # Same count -> degenerate -> None.
        self.assertIsNone(ss.slope_kb_per_request({"physical_kb": 1}, {"physical_kb": 2}, 10, 10))
        # One metric missing -> that metric's slope is None, others still reported.
        seg = ss.slope_kb_per_request(
            {"physical_kb": None, "rss_kb": 1}, {"physical_kb": 2, "rss_kb": 2}, 10, 20)
        self.assertIsNone(seg["physical_kb_per_req"])
        self.assertEqual(seg["rss_kb_per_req"], 0.1)

    def test_build_request_plan_filters_and_sorts(self):
        cps = ss.build_request_plan(100, 3, [50, 10, 100, 0, 200, "10"])
        self.assertEqual(cps, [10, 50, 100])

    def test_plan_phases(self):
        warm, meas, cps = ss.plan_phases(3, 100, [10, 50, 100])
        self.assertEqual(warm, [1, 2, 3])
        self.assertEqual(meas, list(range(1, 101)))
        self.assertEqual(cps, [10, 50, 100])


class TestDecodeError(unittest.TestCase):
    def test_openai_error_json(self):
        body = json.dumps({"error": {"message": "Missing 'input' or 'voice'",
                                     "type": "invalid_request_error"}}).encode()
        self.assertIn("invalid_request_error", ss.decode_error(body))
        self.assertIn("Missing 'input' or 'voice'", ss.decode_error(body))

    def test_server_error_type(self):
        body = json.dumps({"error": {"message": "boom", "type": "server_error"}}).encode()
        self.assertEqual(ss.decode_error(body), "type=server_error message=boom")

    def test_raw_and_empty(self):
        self.assertEqual(ss.decode_error(b"not json {"), "not json {")
        self.assertEqual(ss.decode_error(b""), "(empty body)")


class TestTerminateOwned(unittest.TestCase):
    def test_terminate_then_wait(self):
        proc = mock.MagicMock()
        proc.poll.return_value = None
        proc.wait.return_value = 0
        ss.terminate_owned(proc, grace=1.0)
        proc.terminate.assert_called_once()
        proc.wait.assert_called_once_with(timeout=1.0)
        proc.kill.assert_not_called()

    def test_already_exited_is_noop(self):
        proc = mock.MagicMock()
        proc.poll.return_value = 0
        ss.terminate_owned(proc, grace=1.0)
        proc.terminate.assert_not_called()
        proc.wait.assert_not_called()

    def test_timeout_escalates_to_kill(self):
        proc = mock.MagicMock()
        proc.poll.return_value = None
        proc.wait.side_effect = [ss.subprocess.TimeoutExpired("p", 1.0), 0]
        ss.terminate_owned(proc, grace=1.0)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()


class TestVariedText(unittest.TestCase):
    def test_fixed_returns_base(self):
        self.assertEqual(ss.text_for_request(7, "base base", "fixed"), "base base")

    def test_varied_deterministic_and_distinct(self):
        base = "This is a probe."
        a = ss.text_for_request(1, base, "varied")
        b = ss.text_for_request(2, base, "varied")
        self.assertNotEqual(a, b)               # fresh text each request
        self.assertNotIn(str(2), a)             # only its own number embedded
        self.assertIn("1", a)
        # deterministic from the index
        self.assertEqual(ss.text_for_request(1, base, "varied"),
                         ss.text_for_request(1, base, "varied"))

    def test_varied_keeps_length_near_constant(self):
        base = ("This is a periodic memory probe of the local TTS service. "
                "It should stay clear and steady.")
        lens = {len(ss.text_for_request(i, base, "varied")) for i in range(1, 101)}
        # short template: adding a request number must not balloon output length
        self.assertLessEqual(max(lens) - min(lens), 5)


class TestCliDryRun(unittest.TestCase):
    def test_dry_run_argparse_and_no_exec(self):
        # --dry-run must parse and NOT spawn anything (no Popen, no socket bind,
        # no HTTP). We run main in a subprocess-free path by parsing only.
        args = ss.parse_args(["--dry-run", "--port", "42999",
                              "--output-dir", "/tmp/x", "--requests", "20",
                              "--checkpoints", "10,20"])
        self.assertTrue(args.dry_run)
        self.assertEqual(args.port, 42999)
        self.assertEqual(args.requests, 20)
        # The plan builder must work for the dry-run path without touching FS.
        warm, meas, cps = ss.plan_phases(args.warmup, args.requests, args.checkpoints.split(","))
        self.assertEqual(cps, [10, 20])


class TestLoopbackListeners(unittest.TestCase):
    # Exact `lsof -nP -Fnp -sTCP:LISTEN` output: `p<pid>` record start, `f<fd>`
    # (ignored), `n<host>:<port>` name (no TCP/(LISTEN) decoration). This mirrors
    # a real listener on this Mac (e.g. the production job prints `p98779 f3
    # n*:8081`).
    @staticmethod
    def _lsof(name_field, pid=12345):
        return "p%d\nf%d\nn%s\n" % (pid, 3, name_field)

    def test_parse_loopback_exact_real_output(self):
        lis = ss.parse_lsof_listeners(self._lsof("127.0.0.1:8081", 12345), 8081)
        self.assertEqual(len(lis), 1)
        self.assertEqual(lis[0]["pid"], 12345)
        self.assertEqual(lis[0]["host"], "127.0.0.1")

    def test_parse_all_interface_is_star(self):
        lis = ss.parse_lsof_listeners(self._lsof("*:8081", 12345), 8081)
        self.assertEqual(lis[0]["host"], "*")

    def test_parse_wildcard_real_output(self):
        # Exactly what `lsof -nP -Fnp` returns for the production wildcard listener.
        lis = ss.parse_lsof_listeners("p98779\nf3\nn*:8081\n", 8081)
        self.assertEqual(len(lis), 1)
        self.assertEqual(lis[0], {"pid": 98779, "host": "*"})

    def test_parse_multiple(self):
        text = self._lsof("127.0.0.1:8081", 12345) + self._lsof("*:8081", 999)
        lis = ss.parse_lsof_listeners(text, 8081)
        self.assertEqual(len(lis), 2)

    def test_parse_empty(self):
        self.assertEqual(ss.parse_lsof_listeners("", 8081), [])
        # lsof rc=1 -> empty stdout; no data lines.
        self.assertEqual(ss.parse_lsof_listeners("p98779\nf3\n", 8081), [])

    def _confirm(self, name_field, listener_pid=12345, own_pid=12345, lsof_out_rc=0, port=8081):
        out_text = self._lsof(name_field, listener_pid) if listener_pid is not None else ""
        fake = mock.MagicMock()
        fake.returncode = lsof_out_rc
        fake.stdout = out_text
        fake.stderr = ""
        with mock.patch.object(ss.subprocess, "run", return_value=fake), \
             mock.patch.object(ss.shutil, "which", side_effect=lambda n: "/usr/bin/lsof" if n == "lsof" else "/usr/bin/footprint"):
            return ss.confirm_loopback_owner(port, own_pid)

    def test_owner_ok_loopback(self):
        rec = self._confirm("127.0.0.1:8081", listener_pid=12345, own_pid=12345)
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["host"], "127.0.0.1")
        self.assertEqual(rec["pid"], 12345)

    def test_owner_foreign_pid(self):
        rec = self._confirm("127.0.0.1:8081", listener_pid=999, own_pid=12345)
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["reason"], "foreign_pid")
        self.assertEqual(rec["pid"], 999)
        self.assertEqual(rec["expected_pid"], 12345)

    def test_owner_not_loopback(self):
        rec = self._confirm("*:8081", listener_pid=12345, own_pid=12345)
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["reason"], "not_loopback")

    def test_owner_lsof_unavailable(self):
        with mock.patch.object(ss.shutil, "which", return_value=None):
            rec = ss.confirm_loopback_owner(8081, 12345)
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["reason"], "lsof_unavailable")

    def test_wait_safe_listener_retries_then_ok(self):
        # First poll: not bound yet; second: our pid owns loopback.
        proc = mock.MagicMock()
        proc.poll.return_value = None
        not_bound = {"ok": False, "reason": "no_listener"}
        bound = {"ok": True, "host": "127.0.0.1", "pid": 12345}
        with mock.patch.object(ss, "confirm_loopback_owner",
                               side_effect=[not_bound, not_bound, bound]):
            ok, rec, _ = ss.wait_for_safe_listener(8081, proc, 12345, start_timeout=10, poll=0.01)
        self.assertTrue(ok)
        self.assertEqual(rec["host"], "127.0.0.1")

    def test_wait_safe_listener_foreign_pid_fails(self):
        proc = mock.MagicMock()
        proc.poll.return_value = None
        with mock.patch.object(ss, "confirm_loopback_owner",
                               return_value={"ok": False, "reason": "foreign_pid"}):
            ok, rec, _ = ss.wait_for_safe_listener(8081, proc, 12345, 10, 0.01)
        self.assertFalse(ok)
        self.assertEqual(rec["reason"], "foreign_pid")

    def test_wait_safe_listener_process_exit(self):
        proc = mock.MagicMock()
        proc.poll.return_value = 1
        ok, rec, _ = ss.wait_for_safe_listener(8081, proc, 12345, 10, 0.01)
        self.assertFalse(ok)
        self.assertEqual(rec["reason"], "process_exited")

    def test_wait_safe_listener_timeout(self):
        proc = mock.MagicMock()
        proc.poll.return_value = None
        with mock.patch.object(ss, "confirm_loopback_owner",
                               return_value={"ok": False, "reason": "no_listener"}):
            ok, rec, _ = ss.wait_for_safe_listener(8081, proc, 12345, start_timeout=0.05, poll=0.01)
        self.assertFalse(ok)
        self.assertEqual(rec["reason"], "timeout")

    def test_no_new_bind_flags_in_command(self):
        # The bind fix is a default-constant change, NOT a new CLI flag, so the
        # soak command must carry no --host/--bind flag at all.
        cmd = ss.build_server_command(
            "/bin/pocket-tts", 42999, "/m/models", "/m/t", "/tmp/v")
        self.assertNotIn("--host", cmd)
        self.assertNotIn("--bind", cmd)
        self.assertFalse(any(a.startswith("--host") or a.startswith("--bind") for a in cmd))

    def test_prereqs_require_lsof(self):
        with mock.patch.object(ss.shutil, "which", side_effect=lambda n: None):
            problems = ss.validate_prereqs(Path("/bin/tts"), Path("/m"), Path("/m/t"),
                                           Path("/v"), "example.wav")
        kinds = [k for k, _ in problems]
        self.assertIn("error", kinds)
        # No source dir checks pass here (binary/models/voices missing), but it
        # must not crash and must include the lsof requirement.
        self.assertTrue(any("lsof" in m for k, m in problems))


if __name__ == "__main__":
    unittest.main(verbosity=2)
