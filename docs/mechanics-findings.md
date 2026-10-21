# Realistic drum verification findings

Measured on 2026-10-20 (America/Indianapolis) for issue
[#16](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/16).
These are emulator findings, pending comparison with a physical display.

## Golden input blocker

The checksum-pinned QEMU revision `e39508f79cb0681bbe15cfcebd40a3f45cdc3c48`
builds successfully with the updated C integration. All 301 native/Python tests,
Python lint/format checks and C formatting pass, including GitHub's Fast checks.

The published `v1.4.0` factory download no longer matches
`tools/golden_firmware.json`. Two downloads returned 4,128,768 bytes with SHA-256
`06754c550e4800f40c4bbe2c660b874501c447b9864c7383d93c80db04e5654c`, rather than
the expected `a087801e2ba5e08154af28a636c310b988b43417f28c7ddb7bae4f3df9f6221e`.
The cause of this mismatch has not been established. The golden verifier refuses
the input; the six golden CI jobs fail at that fetch/checksum step, after their
QEMU integration builds succeed. The verified runtime image job is consequently
skipped. No pin, version exception, or known-issue baseline was changed.

Full pinned-golden scenario re-verification remains blocked until the exact
original input is available or the project explicitly reviews a new pin.

## Exploratory runs with the currently published input

The different download was saved separately as
`firmware/published-v1.4.0-06754c55.bin`. Its firmware banner reports
`v1.4.0-rc.1`; the step-based smoke runs explicitly expect that version, with no
golden version exception and no known-issue baseline. The legacy boot controls
record the banner but do not assert its version. None establishes a golden pass.
All runs use seed 7, fresh private flash, `--timing ci`, the same QEMU build,
and the shipped hardware presets. Mechanics and landing expectations were not
adjusted to obtain a pass.

```sh
flapsim run --firmware firmware/published-v1.4.0-06754c55.bin \
  --hardware esp32-8-single-realistic --scenario release-smoke-8 \
  --var FIRMWARE_VERSION=v1.4.0-rc.1 --timing ci \
  --out runs/issue16-realistic-8
flapsim run --firmware firmware/published-v1.4.0-06754c55.bin \
  --hardware esp32-16-dual-realistic --scenario release-smoke-16 \
  --var FIRMWARE_VERSION=v1.4.0-rc.1 --timing ci \
  --out runs/issue16-realistic-16
```

| Layout | Settled homing blank: maximum absolute error | Initial X display | Outcome |
|---|---:|---|---|
| Realistic 8 | 0.290766 pitches at guest 26.674 s | All nearest characters X, worst error 0.261403 at guest 33.546 s | `wait_display` rejects the misaligned X; step 3 times out |
| Realistic 16 | 0.327112 pitches at guest 32.594 s | Not reached before the step deadline | Step 3 times out; homing blank also exceeds the alignment band |

The 8-module X landing fails specifically at bus 0, address `0x21`: true
rotation 2039 steps, magnet at 732, flap-zero offset 2, final angle 1339.
`(1339 - 2) * 37 / 2039 = 24.261402648357038`; nearest flap 24 is X, but the
signed error exceeds the fixed quarter-pitch band. This was accepted by the old
nearest-character-only assertion. The 16-module timeout alone is not proof of an
X landing defect: this two-CPU workspace had not reached that settlement yet.

Both runs exit 4 (timeout), preserve the source firmware hash, and cleanly reap
QEMU with return code 0. Neither has stalls or anomalies. Final counters are
45,584 forward / 8 backward steps for eight modules and 92,360 forward / 16
backward steps for sixteen. The existing one-backward-transition-per-module
behavior remains visible.

## Idealized initial-display controls

The original `esp32-8-single` and `esp32-16-dual` presets both pass their legacy
`boot-esp32-8-single` / `boot-esp32-16-dual` expectations with the same separately
downloaded input and updated quarter-pitch assertion. A 480-second overall
wall-clock budget accommodates this two-CPU workspace; physical parameters and
expected text are unchanged. These controls cover initial X settlement, not the
full HTTP/settings smoke scenario.

| Layout | Result | Wall seconds | Maximum absolute X error |
|---|---|---:|---:|
| Idealized 8 | Exit 0, `XXXXXXXX` | 129.085 | 0.136719 pitches |
| Idealized 16 | Exit 0, `XXXXXXXXXXXXXXXX` | 151.029 | 0.154785 pitches |

Both preserve the source hash and record zero stalls/anomalies and QEMU return
code 0. Outputs are under `runs/issue16-ideal-8` and `runs/issue16-ideal-16`.
For example:

```sh
flapsim run --firmware firmware/published-v1.4.0-06754c55.bin \
  --hardware esp32-8-single --scenario boot-esp32-8-single \
  --timeout 480 --timing ci --out runs/issue16-ideal-8
```

Triage: the smaller true rotation and independent physical offsets expose a
firmware-assumption/model-calibration discrepancy. This is not evidence that
the motor phase decoder missed steps. Do not hide it by increasing the true
rotation to 2048, shifting magnets/flaps to make these starts pass, widening the
landing band, or adding a generic expected-timeout baseline. Compare real
magnet/flap release geometry and the firmware's correction path before assigning
the discrepancy to either side (Q10/Q11 in the POC findings remain open).

Detailed local evidence is in each run's `model-config.json`, `run.json`,
`timeline.jsonl`, `state.json`, `activity.json`, `report.md`, `junit.xml`,
`replay.html`, and serial/QEMU logs. Generated evidence and firmware binaries
remain outside version control.
