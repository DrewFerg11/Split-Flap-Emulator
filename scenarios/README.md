# Scenarios

Every file here is a declarative scenario that `flapsim run` replays against the firmware
(real firmware under QEMU, or the firmware built natively) and the simulated hardware.
This page is a catalog: what each scenario checks, where it runs, and how long it takes.
For the schema, step actions, freshness rules and reports, see
[docs/scenarios.md](../docs/scenarios.md).

## Where scenarios run

| Where | Workflow | When |
|---|---|---|
| **PR CI** | `ci-test.yml` | Every pull request and every push to `main`. |
| **Nightly** | `ci-nightly.yml` | 03:17 UTC daily, plus manual dispatch. Slow or redundant coverage that PR checks skip. |
| **Release** | `release.yml` (calls `ci-container.yml`) | On a `v*` tag, before the image is published. Runs the smoke scenarios from inside the built container image. PRs also run that container check. |
| **Manual** | `flapsim run ...` | Not in any workflow. For local use and docs examples. |

Two backends: **QEMU** runs the real v1.4.2 firmware binary, so it is the most faithful but
slow. **Native** builds the firmware source for the host, which is much quicker and supports
extras QEMU cannot (Wi-Fi/MQTT, a controlled wall clock, `--speed`). Scenarios marked
"native only" declare `modes: [native]`.

## How long they take

Durations are wall-clock seconds on GitHub-hosted `ubuntu-24.04` runners, taken from
successful runs on 2026-10-22 to 2026-10-24 (collected 2026-10-24). They are the
scenario step only; QEMU jobs add roughly 20 to 50 s of setup (cached QEMU build), native
jobs about 50 s. Timings drift with runner load, so read them as ballparks.

- **QEMU PR runs:** average over **12** successful `ci-test.yml` runs (range in parentheses).
- **Native PR runs:** the native job runs scenarios in grouped steps. The group time is the
  average over **12** runs (they are very stable, within about 1 s). The per-scenario split
  comes from the `wall_seconds` of **1** run, so it is a single sample.
- **Nightly:** only **1** nightly run exists so far, so those numbers are single samples.
- **Release / container:** average of the 2 successful tag runs that include the container
  check (`v0.2.0-rc.1`, `v0.2.0-rc.2`), as step time.

Durations in the catalog are *wall* time; the scenarios' own `timeout` values are deadlines,
not expected durations.

## Catalog

### Boot and release smoke (the golden gate)

| Scenario | What it exercises | Runs in | Avg duration |
|---|---|---|---|
| `release-smoke-8.yaml` | Eight modules on one bus. Banner and access-point IP, all drums homed (`XXXXXXXX`), firmware version over serial and `GET /version`, no unexpected errors. Then `POST /settings` to drop the absent second bus, a real reboot, persisted settings, `WIFI ERR` with no Wi-Fi, and a centered `HELLO` through `POST /text`. | PR CI (QEMU and native), release container (QEMU and native) | QEMU 146 s (102 to 180), n=12. Native 126 s, n=1 sample (smoke-8 and smoke-16 together: 251 s, n=12). |
| `release-smoke-16.yaml` | Sixteen modules on two buses. Same startup checks, then a centered dual-row `HELLO` / `WORLD` and an explicit identical repeat (idempotent, display must not change). | PR CI (QEMU and native), release container (QEMU) | QEMU 210 s (186 to 226), n=12. Native 125 s, n=1 sample. |
| `boot-esp32-8-single.yaml` / `boot-esp32-16-dual.yaml` | Bounded boot-only check: the drums must reach the all-`X` homed pattern within the deadline. No version or settings assertions. | Manual (documented in [runner.md](../docs/runner.md) and [development.md](../docs/development.md)) | Not run in CI; no timing. |

### Homing and versions

| Scenario | What it exercises | Runs in | Avg duration |
|---|---|---|---|
| `homing-random.yaml` | Seeded random starting positions. Records the historical blank (`        `) settlement, then the final `XXXXXXXX`. Different seeds exercise different starting angles. | PR CI seed 7 (QEMU). Nightly seeds 19 and 42 (QEMU). | Seed 7: 81 s (54 to 89), n=12. Nightly seed 19: 89 s, seed 42: 53 s (n=1 each). |
| `version-check.yaml` | Reads the serial `Firmware:` line and strictly compares it to `FIRMWARE_VERSION`. Used on purpose with wrong and prerelease versions to prove the runner fails them. | PR CI job `negative-versions` (QEMU); release container (wrong-version case) | 12 s (9 to 14) for the whole negative-version proof, n=12. |

### First boot, settings and calibration

| Scenario | What it exercises | Runs in | Avg duration |
|---|---|---|---|
| `first-boot-8.yaml` | A new user on blank flash (eight modules): access-point mode, configuration through `/settings`, one combined save (layout, offset, name, MQTT), reboot with the settings applied, then reset to defaults. | PR CI (native) | 128 s, n=1 sample. First-boot-8 and 16 together: 139 s, n=12. |
| `first-boot-16.yaml` | The same journey with sixteen modules. The default layout is already right, so the save changes identity, MQTT and one offset. | PR CI (native) | 11 s, n=1 sample. |
| `calibration-8.yaml` | Saves eight different wire offsets through `POST /settings`, observes the real reboot, and asserts all eight physical drums land centered. Uses hardware `esp32-8-single-calibration`; see [calibration.md](../docs/calibration.md). | PR CI (QEMU) | 156 s (113 to 193), n=12. |
| `settings-race-8.yaml` | Stress for the SFD#54 settings race: 30 rounds of a `POST /settings` raced against four concurrent `GET /settings`, then read-back. Raced reads return defaults and lost writes keep the old name. | Nightly (native) | Combined with the soak below. |

### Clock, date and MQTT (native only)

| Scenario | What it exercises | Runs in | Avg duration |
|---|---|---|---|
| `mqtt-native-8.yaml` | Provisioned Wi-Fi and a local broker. Home Assistant discovery messages, availability, an MQTT `set` moving the drums, and state publishing. | PR CI (native); release container (native) | 5 s, n=1 sample (with the two clock scenarios: 50 s, n=12). |
| `clock-dst-native-8.yaml` | Time mode under Central European Time across the 2027 spring-forward jump (01:59 followed by 03:00) with a fixed start time. | PR CI (native) | 29 s, n=1 sample. |
| `date-rollover-native-8.yaml` | Date mode across midnight on New Year's Eve: day, month and year all roll over. | PR CI (native) | 15 s, n=1 sample. |
| `clock-soak-native-8.yaml` | Long soak (#24): three guest hours of time mode, one display update a minute (180 moves), run at `--speed 50`. | Nightly (native) | Settings-race plus soak: 223 s, n=1. The soak is nearly all of it. |

### Fault injection (see [hardware-model.md](../docs/hardware-model.md))

| Scenario | What it exercises | Runs in | Avg duration |
|---|---|---|---|
| `fault-stuck-sensor-8.yaml` | With `esp32-8-single-stuck-sensor`, module `0x23`'s hall sensor never sees the magnet, so it cannot home. Every other module must still display correctly. | PR CI (native) | 187 s, n=1 sample. The two fault scenarios together: 375 s, n=12. |
| `fault-module-lost-8.yaml` | With `esp32-8-single-module-lost`, module `0x25` stops answering on I2C 90 guest seconds in. The firmware must log the failed writes and keep driving the other seven modules. | PR CI (native) | 187 s, n=1 sample. |

### Runtime controls (control API steps)

| Scenario | What it exercises | Runs in | Avg duration |
|---|---|---|---|
| `runtime-controls-16.yaml` | Declarative runtime-control steps against sixteen modules: patch a module's mechanics, inject an I2C fault, reset a module, and check the firmware reports the resulting errors. | PR CI (native and QEMU) | Native 16.5 s, n=1 sample. QEMU 324 s, n=2. |
| `runtime-speed-native-16.yaml` (native only) | Runtime speed changes, a module reset and a power cycle keep the chosen speed. | PR CI (native) | 9.8 s, n=1 sample. The two native control scenarios together: 27 s, n=2. |

### Not scenario files, but run beside them

These CI steps use their own verifier scripts, not a file in this directory. They are listed
so the job timings above make sense: native `boot`, `improv-reboot` and `control` cases
(about 33 to 42 s each), the power and fault probes (about 44 to 46 s), and the QEMU
`control-8` / `control-16` jobs (live state, mechanics, power, and for 16 a disconnected
module probe; 120 to 180 s per step). The release container check also runs the smoke
scenarios inside the image: about 400 s per architecture (`amd64` and `arm64`) for the
QEMU smokes, plus about 160 s for the native smoke and MQTT check, on top of the image build.

## Running one yourself

Step-based scenarios that assert a version need `--var FIRMWARE_VERSION=<release-tag>`;
version matching is always strict. Golden CI runs also pass `--known-issues
baselines/v1.4.2-golden.yaml` (QEMU only; native runs strictly), a
checksum-bound list of linked known firmware defects (see #15 and the docs). Example:

```
flapsim run --firmware firmware/splitflap-esp32_n4-v1.4.2-factory.bin \
  --hardware esp32-8-single --scenario scenarios/release-smoke-8.yaml --seed 7 \
  --var FIRMWARE_VERSION=v1.4.2 --known-issues baselines/v1.4.2-golden.yaml --out runs/smoke-8
```
