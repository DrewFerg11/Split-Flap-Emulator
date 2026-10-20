# Hardware model, configuration and output schema (v1)

The production model implements physical I²C expanders, motor phases, drums and
hall sensors. It does not load firmware settings, infer homing commands, or
choose display text. Firmware remains an unmodified input to QEMU.

## Build and verify

Follow [development.md](development.md) to install the Python package and Linux
dependencies, including `libjson-c-dev`. Core tests need a C compiler and JSON-C,
without a QEMU build. The integration adapter additionally compiles against the
exact configured headers of the pinned QEMU checkout.

```sh
python -m pytest tests/test_hardware.py tests/test_model.py
qemu/build.sh --jobs 4
python -m tools.verify_model --hardware esp32-8-single --out runs/model-8
python -m tools.verify_model --hardware esp32-16-dual --out runs/model-16
```

The bounded verifier uses the checksum-pinned golden factory image, private
working flash, CI timing, a 240-second process deadline and supervised shutdown.
It requires the real banner, I²C discovery, the exact expected missing-address
set, a fresh settled X display, and zero stalls/anomalies. It checks complete
serial lines: only the exact fresh-flash `Preferences.cpp:50` / `nvs_open
failed: NOT_FOUND` message and one code-2 write error per absent default module
are allowed. Other errors, extra write errors and wrong error codes fail.
`verification.json` records the serial lines checked before shutdown; a final
partially printed UART line can remain in the retained raw log.

The phase-1 stub has been replaced by `model/adapter.c`. The old
`tools/verify_boot.py` remains a source of shared firmware/cleanup helpers and
foundation regression tests. Phase 3 replaces process management with the CLI;
phase 4 replaces these golden-only assertions with scenarios and reports.

## YAML and normalization

The built-ins are `hardware/esp32-8-single.yaml` and
`hardware/esp32-16-dual.yaml`. A configuration must specify `chip`, `buses`,
`drum`, and `start_angles`; unknown and duplicate YAML fields fail. Only
`esp32`, controller IDs 0/1, unique buses and unique PCF8575 addresses 0x20–0x27
per bus are supported. The reserved I²C ranges and the built-in bus-0 sensor
at 0x48 are rejected with explicit diagnostics. Empty buses are valid; at least
one physical module must exist.

Existing file paths take priority. Only a bare name (optionally ending in
`.yaml`) can resolve to an installed built-in; a missing path with a directory
component never falls back. Load errors preserve the caller's original argument.

`drum` contains integer `steps_per_rotation` (4–1,000,000), integer
`magnet_position` (0 through steps−1), integer `sensor_window` (1 through steps),
and `flaps` (2–95 distinct printable ASCII characters, no more than steps).
These values describe mechanics, independently of the firmware's own settings.

Module ordering is always bus ID then ascending address, independent of YAML
ordering. `start_angles` accepts:

- An integer list with exactly one position per module in that order, in
  `[0, steps_per_rotation)`.
- `{seed: N}`, with an unsigned 32-bit integer. Booleans are rejected.
- `random`, which selects and records a random 32-bit seed.

Seeded positions use xorshift32 (left 13, right 17, left 5, masked to 32 bits),
then modulo steps for each module. Seed zero initializes the generator with
`0x6d2b79f5`. This algorithm is independent of Python/C library PRNG versions.
The built-ins use seed 7; callers can override a valid configuration's starts
through `load_hardware(..., seed=N)`. All resolved angles and the seed are
recorded in the normalized JSON, including when the original input was random.

`write_model_config()` writes `model-config.json` with `schema_version: 1`,
`chip`, `seed`, `drum`, a sorted `modules` array of `{bus, addr, angle}`, and
explicit absolute `outputs` paths. Pass its path in the single environment
variable `FLAPSIM_CONFIG`. `outputs` names `state`, `activity`, `timeline`, and
optional `trace` (null when disabled). The POC's older `FLAPSIM_BUS*` and other
environment variables are not production configuration inputs.

## Hardware behavior

Each module starts with latch `0xFFE1`, unknown rotor phase, and its configured
physical angle. START selects an independent transaction on each controller.
Only configured addresses ACK; absent devices NACK without incrementing model
anomalies. Low/high bytes commit on FINISH only for exactly two written bytes.
Zero-byte probes are valid. Reads return low/high bytes of the latch with bit 15
supplied by the hall sensor; a master's terminating NACK is normal.

Coil masks 3, 9, 12 and 6 (bits 1–4) correspond to phases 0, 1, 2 and 3.
The first energize establishes phase without a full step. Subsequent phase
deltas +1 and −1 move forward/backward with rotation wrapping; an opposite
phase increments stalls. De-energizing loses phase. An invalid nonzero coil
pattern increments an anomaly and keeps the module energized until stopped.
Displayed flap index is `floor((angle * N * 2 + steps) / (2 * steps)) mod N`,
including ties rounding forward and wrap to the blank. Hall detection is
`(angle - magnet_position + steps) mod steps < sensor_window`.

Counters per module and in aggregate are `forward`, `backward`, `stalls`,
`writes`, `reads`, and `anomalies`. Writes/reads count valid two-byte completed
transactions. Malformed lengths, data without the matching START, direction
errors and START while open are diagnosed. The golden boot records one backward
transition per module; the regression gate requires zero stalls and anomalies.

The adapter prepares the physical model before `qemu_init`, registers reset and
virtual-time timer hooks after QEMU's subsystems exist, and keeps the original
launcher thread for initialization/main loop/cleanup. Registering a reset hook
before `qemu_init` creates QOM objects too early; real-boot verification caught
that ordering error during implementation. `--version` and help work without a
hardware configuration. Guest reset aborts open controller transactions while
preserving expander latches, motor phase/energy, angles and counters. A new
process initializes from its normalized configuration. QEMU's BQL serializes
the integration callbacks; direct core API users must serialize their calls.

## Output and freshness contract

`state.json` is atomically replaced through a same-directory temporary file on
initialization and all-module settlement. `move_finished` occurs only when
every configured module on both buses has de-energized. Row 1 stopping while
row 2 remains active cannot finish a movement. This is a physical settlement,
not an inferred firmware command boundary.

`timeline.jsonl` appends and flushes complete JSON records for `initialized`,
`move_started`, `move_finished`, `guest_reset`, `anomaly`, and `snapshot`.
A QEMU virtual-time timer emits snapshots approximately every 100 ms while
any module is energized, including when no new I²C event occurs. It updates
`activity.json` atomically while idle as well, so consumers can observe guest
time and current counters. Optional `i2c.log` records verbose raw bus events.
Output errors fail the integration with a stderr diagnostic and nonzero exit.

State and timeline records contain:

| Field | Meaning |
|---|---|
| `schema_version` | Integer 1 |
| `event` | Event name |
| `guest_ns` | Nonnegative QEMU virtual-clock timestamp in nanoseconds |
| `sequence` | Monotonic emitted-event sequence |
| `movement_id` | Increments when all-off transitions to any energized module |
| `settlement_id` | Increments when the final energized module stops |
| `settled` | True only when all modules are de-energized |
| `text`, `rows` | Flattened text and two rows, preserving every space |
| `counters` | Aggregate nonnegative counters |
| `modules` | Ordered module snapshots |

Each module has integer `bus`, `addr`, `angle`, `latch`, `phase` (−1 or 0–3),
one-character `char`, boolean `energized`/`hall`, and its `counters`.
`activity.json` contains the identity/time/settled/aggregate-counter fields;
its counters can be newer than those in the last physical settlement.

An old `state.json` deliberately remains on disk during motion. Consumers must
check activity, read state, then check activity again; both activity records
must be settled and match the state's movement/settlement IDs. The supplied
`flapsim.state.read_settlement()` implements this and validates module counts,
identities, ordering, mechanical fields, text/rows and structured counters.
Missing or currently moving/stale outputs yield `None`; malformed complete data
raises `ValueError`. Callers waiting for data can retain that reason for timeout
diagnostics. A post-command scenario must additionally require an applicable
new settlement and define its quiet/idempotent-command policy (phase 4).

Freshness is a point-in-time observation ending at the second activity read,
not a lock on the running guest. Motion may begin after that read or before the
caller consumes the result; another final read would only move that boundary.
Callers needing continued settlement must recheck activity and apply their
post-command/quiet policy rather than treating the returned snapshot as a lease.

## Verification evidence

Core/configuration/schema and native launcher tests run without QEMU. Real
golden checks passed locally with eight modules at `XXXXXXXX` and sixteen at
`XXXXXXXXXXXXXXXX`, zero stalls/anomalies, unchanged source hashes and child
exit code 0. Final checks run concurrently on a two-CPU-quota workspace took
224.497s and 226.010s. Artifacts are under `runs/model-8-final/` and
`runs/model-16-final/`. CI repeats both layouts independently with retained
serial/QEMU logs, normalized config, state, activity, timeline and provenance.
The phase remains in progress until its PR is merged and acceptance passes.
