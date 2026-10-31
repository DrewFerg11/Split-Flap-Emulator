# Native-mode checks in Split-Flap Display CI

Plan step 6 of [#24](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/24). The firmware
repository runs one **required** job that builds each pull request's own source in native mode
and runs the emulator's scenarios against it. QEMU mode against the released image stays the
release gate, and a [nightly QEMU job](#nightly-qemu-run) boots `main`'s factory image ahead of it.

The job uses only the **published image**, pinned by digest: it pulls the image, builds the PR's
source inside it with `--network none` (the pinned libraries ship in the image), and runs every
scenario in it. It needs no emulator checkout, no host toolchain and no registry login (the image
is public). [`firmware-native-ci.yml`](firmware-native-ci.yml) mirrors the job as installed in
Split-Flap-Display's `.github/workflows/ci-emulator-native.yml` (**CI / Emulator (native)**, required check **Emulator (native)**). `FLAPSIM_IMAGE` is
`ghcr.io/drewferg11/split-flap-emulator:<version>@sha256:<digest>`: Docker pulls by the digest, and
the tag is there for readers. To move to a newer emulator, update both from the digest that
release's publication recorded.

## What it checks

| Scenario | Layout | Covers |
|---|---|---|
| `release-smoke-8` | `esp32-8-single` | boot to X, version banner and `/version`, hardware settings reboot, `WIFI ERR`, `HELLO` |
| `release-smoke-16` | `esp32-16-dual` | dual-bus boot, `dual-single` HELLO / WORLD, idempotent repeat |
| `mqtt-native-8` | `esp32-8-single` | Wi-Fi + broker from seeded settings, Home Assistant discovery, MQTT `set` → drums, state |
| `clock-dst-native-8` | `esp32-8-single` | time mode in Central European Time across spring-forward |
| `date-rollover-native-8` | `esp32-8-single` | date mode across New Year |

Runs are **strict**: no known-issues baseline applies to PR source (baselines bind only the
clean golden commit). The expected version is whatever the firmware's own `inject_version.py`
rule produces for the PR checkout, read back from `build-info.json`. Every scenario runs even
if an earlier one fails. The job summary lists each outcome, and the reports, JUnit, replays
and serial logs are uploaded as artifacts.

## Nightly QEMU run

Native mode doesn't run the real ESP32 binary, so Split-Flap-Display's
`.github/workflows/ci-emulator-qemu.yml` (**CI / Emulator (QEMU nightly)**) covers that between releases. Daily at 07:23 UTC (or on
manual dispatch) it builds `main`'s `esp32_n4` merged factory image with the same steps as the
release workflow, then boots it under QEMU in the same pinned image for `release-smoke-8` and
`release-smoke-16`, with `--timing ci` and no baseline. The expected version is the firmware's own
`git describe` result, so no tag is needed. It gates nothing; the release still runs its own strict
simulation of the exact image it publishes ([release gate](release-gate.md)). Evidence is uploaded
as `nightly-qemu-evidence` for 14 days. Update its `FLAPSIM_IMAGE` together with the native job's
and the release gate's.

## Expected state today

The released v1.4.1 source fails the strict smoke scenarios on
[SFD#54](https://github.com/DrewFerg11/Split-Flap-Display/issues/54) and
[SFD#84](https://github.com/DrewFerg11/Split-Flap-Display/issues/84). Both were fixed on `main` by
[SFD#91](https://github.com/DrewFerg11/Split-Flap-Display/pull/91), after which every scenario
passed, so the job was made required.
