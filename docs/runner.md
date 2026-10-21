# Isolated firmware runner

Install and build using [development.md](development.md), then run:

```sh
python -m tools.verify_model --fetch-only
flapsim run --firmware firmware/splitflap-esp32_n4-v1.4.1-factory.bin \
  --hardware esp32-8-single --scenario scenarios/boot-esp32-8-single.yaml \
  --out runs/boot-8
python -m tools.verify_model --hardware esp32-8-single --out runs/boot-8
```

Use `esp32-16-dual` and `scenarios/boot-esp32-16-dual.yaml` for sixteen modules.
Hardware accepts a file or a built-in name; scenarios accept a file or `boot`.
`--qemu-prefix` selects the staged runtime (default `build/qemu/install`, or
`FLAPSIM_QEMU_PREFIX`). `--seed` overrides the validated hardware's starting seed
and records every resulting angle. `--trace` additionally writes `i2c.log`.

The phase-3 scenario is a deliberately small bounded boot check:

```yaml
version: 1
mode: boot
timeout: 240
expect_text: XXXXXXXX
```

It waits for the real firmware banner and a fresh settlement after movement,
with no stalls/anomalies and, when specified, exact text in module order.
A settled state with stalls or anomalies fails with exit code 5 immediately,
even when its text differs from the expectation.
`--scenario boot` omits the text assertion; it still requires movement and the
banner. `version`, `mode`, and `timeout` are required. Missing fields report
their names with invalid-input exit code 2 before launch. Unknown keys,
unsupported modes, duplicate YAML keys, invalid deadlines
and text that cannot fit the physical modules fail before launch. Full scenario
steps, version checks, reports and replay are documented in
[scenarios.md](scenarios.md). The separate golden
evidence checker retains the narrowly scoped discovery/error assertions; a
generic boot command does not silently allow or classify firmware serial errors.

Timing selects exactly one mode:

| Mode | QEMU option |
|---|---|
| `ci` (default) | `-icount shift=3,sleep=off` |
| `accurate` | `-icount shift=2,sleep=off` |
| `realtime` | no `-icount` |

The runner does not enable MTTCG. Wall time depends on the host and load; the
scenario's `timeout`, or the explicit `--timeout` override, is a monotonic total
execution limit including preparation and startup attempts. Shutdown gets at
most two additional seconds for TERM and two for KILL/reaping. This bound also
applies on assertion failure, early QEMU exit, Ctrl-C and SIGTERM. The CLI records
signals as cancellation so a second signal cannot interrupt cleanup or create
an ownership gap around process launch.

## Native mode

`--mode native` runs firmware **source** built by `native/build.py` instead of a factory image
(see [native.md](native.md)); QEMU mode stays the default and the release gate.

```sh
python native/build.py --out build/native/golden
flapsim run --mode native --native-build build/native/golden --hardware esp32-8-single \
  --scenario scenarios/release-smoke-8.yaml --var FIRMWARE_VERSION=v1.4.1 --out runs/native-8
```

`--firmware-src golden|git:<ref>|<checkout>` builds first instead (from a source checkout).
`--speed` (0.25–50, default 5) sets the virtual clock rate. Each mode accepts only its own
inputs: `--firmware`/`--timing` are QEMU-only and `--native-build`/`--firmware-src`/`--speed`
native-only, so a run never silently ignores an option. Scenarios, hardware, reports, replay,
deadlines and cleanup are shared. The runner binds the device's port 80 on `127.0.0.1` itself
and the native process inherits the listening socket, so the endpoint never races another
process and survives emulated resets. The manifest records `"mode": "native"`, the firmware
source/ref/commit/dirty state/version with the executable's path, size and SHA-256 (which must
be unchanged afterwards), the native build provenance and speed, and `native_returncode`.
QEMU-mode manifests now record `"mode": "qemu"`.

## Isolation and lifecycle

The runner rejects empty or larger-than-4-MiB firmware, unsupported hardware,
missing executable/library/ROM files, invalid build provenance and an occupied
output directory. A per-directory exclusive ownership file prevents concurrent
use. Each run copies the input into its own `flash.bin`, pads with `0xff` to
exactly 4 MiB and checks the original SHA-256 again after shutdown. An in-process
guest reset retains this flash and physical model. A new run always starts from
the original input. The runner never restarts an unexpectedly exited emulator.

QEMU runs in its own session/process group on Linux; the runner becomes a child
subreaper and waits for adopted descendants in that group. TERM/KILL targets only
that group, including when its original leader exits first. Detached programs
that deliberately escape the emulator's process group are outside this runtime
contract. No global process killing is used.

HTTP forwarding binds only `127.0.0.1`, targeting guest `192.168.4.1:80` on
AP-mode SLIRP. A temporary port probe supplies a candidate; QEMU must actually
bind it. Forwarding-bind failures can retry with a fresh candidate, up to five
total startup attempts within the same deadline. Every attempt is reaped and
logged. Each retry resets
the private flash/model output so incomplete startup cannot seed the next run.
Other startup errors fail immediately. A successful QMP `query-status` reporting
`running` proves QEMU initialization finished before invoking the executor;
the guest's HTTP server may still be starting. Runtime failures after that point
end the run. QMP uses a private short-lived Unix socket, avoiding a second TCP
port and long output-path socket limits.

## Executor contract

`flapsim.executor.Executor` supplies a validated, JSON-serializable `document`,
a finite positive `timeout`, and `execute(context) -> dict`. Preparation must
validate all step syntax before launching. The runner owns firmware, process,
environment, ports, cleanup and the overall deadline. A scenario must not launch
or replace QEMU, recopy flash on guest reboot, or extend `context.deadline`.

`RunContext` exposes the output path, normalized hardware, loopback HTTP
`endpoint` and monotonic deadline. `checkpoint()` checks cancellation, early exit
and timeout; `pause()`, `serial()` and `settlement()` also check it. Executors
must use these checks between actions and bound every blocking operation by the
remaining deadline. `settlement()` uses the activity/state/activity freshness
contract: missing files mean keep waiting, moving/obsolete state cannot pass,
and malformed model data fails. Return JSON evidence only after all assertions
pass; raise `ExecutionFailed` for an assertion and `TimeoutError` for a timeout.
The runner performs a final process check, cleanup, complete state/timeline
validation and original-input hash check before recording success.

## Results

| Exit | Meaning |
|---|---|
| 0 | executor completed; final artifacts/input check passed; child group reaped |
| 2 | invalid CLI/input/configuration/runtime before launch |
| 3 | launch, QEMU/runtime, output, cleanup or source-integrity failure |
| 4 | total deadline exceeded |
| 5 | executor assertion failed |
| 130 / 143 | SIGINT / SIGTERM cancellation |

`run.json` is written atomically before launch, at executor start and after
cleanup. It records resolved hardware/angles/seed, source checksum/size, full
build provenance, timing, resolved scenario, total wall time, chosen endpoint,
exact commands, PIDs and return codes for every startup attempt, result or
failure context, exit status and the post-run input hash result. Errors found
before claiming a fresh output directory print a diagnostic with exit 2.

Inspect `serial.log`, `model-config.json`, `state.json`, `activity.json`,
`timeline.jsonl`, `qemu-attempt-N.log`, `run.json` and optional `i2c.log`.
Golden checks add `verification.json`. Preserve a failed directory and use a new
`--out` to retry. CI uploads these diagnostics on failure as well as success;
firmware/private flash is excluded. Fast checks use controlled child processes
and actual listeners to verify port retries, parallel isolation, hangs, early
exit, TERM/KILL escalation, descendant reaping, signal handling and malformed
output. CI separately runs the actual published firmware through this CLI.
