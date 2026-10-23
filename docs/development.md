# Development and model verification

The verified build host is Linux x86-64 with Python 3.11 or newer. The integration
runs the production physical model through an unmodified pinned QEMU library.
Phase 3 adds the supervised `flapsim run` CLI; see [runner.md](runner.md) for its
executor, isolation, exit-code and evidence contracts.
The POC remains frozen under `poc/` (only its tracked bytecode cache was removed).

On Debian/Ubuntu, install system build dependencies as an administrator:

```sh
sudo apt-get update
sudo apt-get install -y --no-install-recommends build-essential git ninja-build \
  libglib2.0-dev libgcrypt20-dev libslirp-dev libpixman-1-dev \
  python3-venv flex bison pkg-config libjson-c-dev
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,control]' clang-format==18.1.8
```

Run the same fast checks as CI:

```sh
flapsim --help
ruff check .
ruff format --check .
clang-format --dry-run --Werror model/*.c model/*.h qemu/*.c qemu/*.h tests/fixtures/*.c
python -m pytest
```

The native model/configuration/schema checks run without QEMU. Tests are
nonempty and normal pytest failures (including collection failures and an
accidentally empty suite) are never suppressed.

Build and verify the real firmware:

```sh
mkdir -p runs
set -o pipefail
qemu/build.sh --jobs 4 2>&1 | tee runs/qemu-build.log
python -m tools.verify_model --fetch-only
flapsim run --firmware firmware/splitflap-esp32_n4-v1.4.1-factory.bin \
  --hardware esp32-8-single --scenario scenarios/boot-esp32-8-single.yaml --out runs/model-8
python -m tools.verify_model --hardware esp32-8-single --out runs/model-8
flapsim run --firmware firmware/splitflap-esp32_n4-v1.4.1-factory.bin \
  --hardware esp32-16-dual --scenario scenarios/boot-esp32-16-dual.yaml --out runs/model-16
python -m tools.verify_model --hardware esp32-16-dual --out runs/model-16
```

The build fetches precisely `qemu/QEMU_COMMIT` from the upstream lcgamboa fork,
builds only `xtensa-softmmu`, preserves `-fPIC`, and enables TCG, system emulation,
gcrypt and SLIRP. Other optional features are disabled using upstream's configure
feature list. QEMU's pinned Meson wraps fetch their pinned subproject revisions
on the first build. No source patch is applied. It first builds the upstream
executable to obtain its exact link flags, then relinks without `system_main.c.o`
as `libqemu-xtensa.so`, following upstream's shared-library recipe. Only the
shared-library launcher is the supported runtime integration. It links the
JSON-C hardware core and its thin timer/reset adapter; the adapter compiles
against that exact checkout's configured headers. Hardware/schema details and
the phase-1 probe replacement path are in [hardware-model.md](hardware-model.md).

Outputs under `build/qemu/`:

| Path | Purpose |
|---|---|
| `src/` | Exact upstream source checkout, including pinned Meson subprojects |
| `work/` | Configured Ninja build, configuration stamp, configure/Meson logs |
| `install/bin/flapsim-qemu` | Standalone callback launcher; accepts QEMU arguments |
| `install/lib/libqemu-xtensa.so` | Unmodified pinned QEMU, linked as a library |
| `install/share/qemu/esp32-v3-rom*.bin` | ESP32 v3 ROM images from that checkout |
| `install/build-info.json` | Revision, flags, toolchain, integration digest, build time |

Repeat `qemu/build.sh` for an incremental build. Configuration changes invalidate
the configure stamp; integration sources are compiled on every invocation. The
script refuses source modifications or an unexpected revision. Use a new
`--build-root` when changing pins, or remove the old generated tree first.
`qemu/build.sh --clean` removes only the generated `work/` and `install/` trees;
it preserves the source fetch. For a fully cold build, remove `build/qemu/`.
`--jobs` must be positive. Use `PYTHON=/absolute/path/to/python` to select Python.
`--install-dir <path>` stages a separate runtime while reusing a compatible
`--build-root`. Stop users of an installation before replacing that runtime.

The fetch command downloads the exact published factory artifact named in
`tools/golden_firmware.json` (v1.4.1, esp32_n4). Its 4,128,768-byte input must hash
to `5a71579b127ee87f0716cb1df938c9f98d3519a9283f72c003dfb857db32e6f4`.
The checksum was measured from the published release download; it is a regression
pin, not a separate upstream signature. Existing corrupt inputs fail instead of
being silently replaced. A separate working copy is padded with `0xff` to
4,194,304 bytes. The original input is verified again after supervised shutdown.

Verification requires the firmware banner and actual discovery, the exact
expected missing-address set, and a fresh all-module X settlement with zero
stalls/anomalies. The eight-module golden boot allows only its known default
bus-1 write failures and the exact benign fresh-NVS error; unrelated serial
errors fail. The CLI owns process management; the golden checker only inspects
completed evidence. The CLI terminates and reaps the emulator group on success,
failure, timeout, Ctrl-C or SIGTERM, with bounded TERM/KILL escalation. Its output
directory must be empty. Inspect `serial.log`, `qemu-attempt-N.log`, `run.json`,
`flash.bin`, `verification.json`, `model-config.json`, `state.json`, `activity.json`
and `timeline.jsonl` in each output directory. Repeat with a new
output directory, or deliberately remove the old generated run.

Boot verification does not assert versions; the Phase 4 scenarios add strict
serial/HTTP version assertions.

The slow, soak-style scenarios (native `settings-race-8` and `clock-soak-native-8`, homing
seeds 19 and 42) run in the scheduled `CI / Nightly` workflow, which can also be started
with `workflow_dispatch`; a failed nightly run is the signal. Shared build steps live in
`.github/actions/native-setup` and `.github/actions/qemu-setup`.

CI runs independent `Fast checks` and a `Pinned QEMU build and boot` matrix
(explicit suffixes `esp32-8-single` / `esp32-16-dual`) on PRs, main pushes and
manual runs. Both layouts retain diagnostics even if one fails. Cache keys include the QEMU pin, build scripts,
workflow configuration, exact toolchain and integration sources. A fallback can
reuse the QEMU compilation only when the pin, toolchain and build configuration
match; model/integration source changes still rebuild the launcher before verification
and regenerate its provenance. Cached staged binaries are overwritten by that
build, and a failed build prevents the boot step. Build provenance distinguishes
cold from incremental invocations and records host architecture and visible CPUs.
Every invocation checks source provenance and rebuilds the launcher. Jobs and emulator processes
are bounded; superseded runs cancel. Available build, serial and verification
logs and model timeline/state are retained for 14 days even on failure. Firmware/flash binaries are not
uploaded. CI has read-only repository permissions and is independent of the
firmware repository's future trusted release gate.

## Emulation pitfalls

Lessons from building the emulator that still apply when writing scenarios or tooling:

- **Define "settled" carefully.** A move has finished only when **all** modules have de-energized their coils, ideally followed by a short quiet period. Snapshotting when the first module releases its coils can catch other rows mid-move.
- **Wait for the right serial markers.** `AP IP Address` is printed again on MQTT reconnects, not only at boot; use `Reset reason: software reset` to detect reboots. `AP IP Address` also contains the substring `IP Address`, so anchor regexes (`(?m)^IP Address`).
- **The firmware is busy during startup.** HTTP requests sent during homing can take more than 20 s to be answered under emulation. Wait for the startup display before driving it.
- **Use per-run host ports.** A leftover QEMU holding `hostfwd` ports makes the next one fail with "Could not set up host forwarding rule".
- **Do not `pkill -f` a pattern** that also appears in your own command line.
- **A model reset is not a firmware reset.** Drum state survives a guest reboot, as on real hardware.
- **One transient I²C error latches `hasErrored`**, permanently disabling hall reads (and so homing) for that module. Fault-injection tests should expect that.
- Do not assert on exact timestamps: emulated timing is repeatable but not bit-for-bit deterministic. Assert on settled states.

Earlier findings are in [`history/poc-findings-and-mvp-plan.md`](history/poc-findings-and-mvp-plan.md).
