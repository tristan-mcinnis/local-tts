# Local TTS follow-up, 2026-09-06

## Completed result

The isolated repeated-use test completed **100/100 requests**, with no failures
or timeouts, in 30.68 seconds. Three warmups preceded the measured requests.

Physical footprint at the four checkpoints:

- After warmup: **1186 MiB**.
- After 10 requests: **1187 MiB**.
- After 50 requests: **1188 MiB**.
- After 100 requests: **571 MiB**.

These are physical-footprint readings, not RSS. This bounded run did not show
sustained growth. It does not establish that every long-running workload is
leak-free. The final drop is observed, not attributed to a particular cause.
There is no before/after speed claim, battery claim, or real-use responsiveness
claim. Text varies by request, so timings are not a throughput comparison.

Raw evidence: [tts-soak.json](performance/20260906-followup/tts-soak.json).
The separate fleet background observation lasted 15 minutes; this request-count
probe did **not** run for 15 minutes. Its 900-second setting is a deadline.

## Runtime correction and deployment

The user explicitly approved restricting TTS to this Mac. `TTSServer::start()`
now uses `htonl(INADDR_LOOPBACK)` instead of `INADDR_ANY`. The startup message
names `127.0.0.1` and flushes immediately so readiness logs work when redirected.
No audio generation, thread, temperature, trimming, or model settings changed.

The tested binary was installed through `scripts/install-runtime.sh`. The old
service was stopped before copying its mapped runtime files. The only observed
client beforehand was Local Models' health monitor; server CPU was idle.

Verified after deployment:

- The sole port-8081 listener is **127.0.0.1:8081**, owned by the new TTS process.
- `/health` responds successfully.
- Installed SHA-256:
  `22febd35ee3fd5fb5c10661f21c3aed28706dac38516fb17e6e9605b613da369`.
- The launchd plist and every existing voice WAV hash are unchanged.
- The prior runtime, plist, and checksum manifest are retained under
  `~/Library/Application Support/HousePerformance/rollback-followup-20260906/`.
  The old binary's source revision is recorded as unknown.

Restoring that backup would restore the former wildcard/LAN listener too. Treat
rollback as a deliberate security trade, not an automatic response to a test
failure. Stop the job before restoring runtime files and restart afterward.

## Repeatable isolated probe

```bash
python3 tools/soak_service.py --dry-run
python3 tools/soak_service.py \
  --binary ./pocket-tts \
  --output-dir /tmp/house-tts-soak \
  --requests 100 --warmup 3 --checkpoints 10,50,100 \
  --text-mode varied --max-seconds 900
```

The harness does not build, install, restart, or contact the production service.
It uses a disposable process on an ephemeral loopback port, not 8081. Before
HTTP it checks structured `lsof -Fnp` output for exactly its own listener on
loopback. Wildcard addresses, foreign owners, multiple listeners, and absent
lsof fail closed. A successful health request alone is insufficient proof.

Weights and tokenizer are read from `~/Models/pocket-tts`. Only the stock
`example.wav` is copied into the isolated output directory; voice caches go
there, never into production. Production audio defaults remain enabled. The
measured text includes a deterministic request number to avoid measuring only
repeated identical requests. Returned WAV data is checked in memory, not played
or retained. The copied stock voice and generated caches remain in the output
directory for inspection. No private voice audio is included in the evidence.

Cleanup signals only the PID the harness spawned. Exit 0 means complete;
nonzero means preflight failed or the configured completion criteria were unmet.
Footprint collection is best-effort and explicitly marks RSS-only fallback;
this recorded run obtained physical footprint at all four checkpoints.

## Validation

- **51** harness unit tests and **26** existing benchmark unit tests passed.
- Actual CTest: **2/2 passed**, including native synthesis and loopback bind plus
  health verification. The bind smoke copies stock voice caches into temp space.
- Independent static review passed after correcting the shell lsof parser.
- Independent execution confirmed 100 successful requests, owned-loopback
  isolation, and removal of the spawned process. Production remained untouched
  during measurement; deployment was a separate parent action afterward.

The first live smoke run exposed a buffered readiness log. Flushing the startup
line fixed that test failure. A review claim that `htonl(INADDR_LOOPBACK)` was
wrong was independently disproved with a host-compiled bind test and retracted.
The correct byte-order conversion was preserved.
