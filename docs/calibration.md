# Physical calibration

The firmware must calibrate against the model's physical drum and hall sensor;
the model never reads `wireOffsets`, `wire1Offsets`, or `magnetPosition` from
firmware settings. Those settings influence the motor commands emitted by the
unmodified firmware, while the model keeps its original true geometry across
guest resets. Resolved values are recorded in `model-config.json`, not supplied
to the guest. See [hardware-model.md](hardware-model.md) for YAML overrides and
seed resolution.

The realistic presets use seeded per-module rotation, magnet position, flap
zero, sensor width and polarity. The original presets and explicit `*-perfect`
presets retain the firmware's idealized defaults. The fixed
`esp32-8-single-calibration` fixture uses eight 2048-step drums to isolate sensor
and flap calibration from the independent gearbox-ratio error tested by #16.

## Electrical edges and offsets

`magnet_position` is physical entry into the sensor window, and `flap_offset`
is the absolute physical center of flap zero. Bit 15 goes high on entry for an
active-high sensor and on exit for an active-low sensor. The pinned firmware
detects a high electrical level; it has no polarity setting. Calibrate its
reference against the relevant edge rather than secretly changing sensor
polarity in the model.

For rotation R, magnet entry M, window W and flap zero F:

```text
edge = M                         # active-high
edge = (M + W) mod R             # active-low
reference = (edge - F) mod R
wireOffset = reference - magnetPosition - displayOffset
```

This is the ideal edge reference. Firmware polling and integer step rounding
can leave a small residual error; the scenario uses #16's unchanged quarter-
pitch acceptance band and records the actual fractional errors. This is not a
measurement of a real flap's release threshold. A real build still needs
physical calibration.

The fixture uses firmware `magnetPosition=730`, `displayOffset=0`:

| Address | Magnet M | Window W | Polarity | Flap zero F | Wire offset |
|---|---:|---:|---|---:|---:|
| 0x20 | 650 | 24 | active-high | 130 | -210 |
| 0x21 | 680 | 28 | active-low | 138 | -160 |
| 0x22 | 710 | 32 | active-high | 90 | -110 |
| 0x23 | 740 | 36 | active-low | 106 | -60 |
| 0x24 | 770 | 40 | active-high | 30 | 10 |
| 0x25 | 800 | 44 | active-low | 54 | 60 |
| 0x26 | 830 | 48 | active-high | 2038 | 110 |
| 0x27 | 860 | 52 | active-low | 22 | 160 |

The geometry and settings are checked-in independently, with a test checking
their relationship. No runtime helper generates passing offsets from model
output. Changing the start seed changes the initial angles, not this fixture's
explicit physical values.

## Run the firmware scenario

```sh
python -m tools.verify_model --fetch-only
flapsim run --firmware firmware/splitflap-esp32_n4-v1.4.1-factory.bin \
  --hardware esp32-8-single-calibration --scenario calibration-8 \
  --var FIRMWARE_VERSION=v1.4.1 --known-issues baselines/v1.4.1-golden.yaml \
  --out runs/calibration-8
```

The scenario waits for the initial `Homing` message, saves only the eight
`wireOffsets` through `POST /settings`, and reads back the exact offset CSV.
It then waits for a software reset and AP readiness and requires `XXXXXXXX`
with every drum centered, followed by model/serial error checks. The firmware's
unused default second bus remains absent; each boot must produce exactly the known eight
missing-address errors. No wiring or mode setting is changed to force a result.

The settings exchange is deliberately synchronized with the initial homing
window: `display.init()` has finished reading settings, while `loop()` has not
yet begun its competing Preferences accesses. This isolates calibration from
the known SFD #54 race. Readback happens before reset while that window is open;
the centered display after reset additionally checks that firmware loaded and
used the saved offsets. This scenario does not validate concurrent settings
access in a fully booted, idle device, nor claim to fix that firmware bug.

CI runs the same scenario. The existing checksum-bound baseline may tolerate
its documented serial noise, but contains no expected failure for
`calibration-8`: missing persisted offsets, wrong characters, partial landings
or a timeout remain failures. The settings-store race tracked by SFD #54 can
therefore fail this scenario even when POST returned success. Do not change
physical values, relax alignment, or add a generic timeout allowance to conceal
that firmware defect. Retain `run.json`, `model-config.json`, `state.json`,
`timeline.jsonl`, `report.md`, `junit.xml`, `replay.html` and serial logs.

## Verification findings

The v1.4.1 run with seed 7 passed after a real software reset: all eight drums
settled at `X`, the largest absolute error was 0.15478515625 flap pitches
(below 0.25), and the model recorded zero stalls and anomalies. All 339
native/Python tests passed, including electrical polarity/window boundaries,
reset preservation, deterministic overrides and the packaged presets/scenario.

Two earlier diagnostic runs submitted settings after startup had completed.
Both returned HTTP success but lost the offsets across reset, leaving
`TUVWXYZ0` instead of `XXXXXXXX` and failing the strict display assertion.
Those failures exposed the existing Preferences race and motivated the homing
synchronization above; no geometry, alignment threshold or baseline was changed
to accept them.
