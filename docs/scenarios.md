# Firmware scenarios and evidence

Build/install using [development.md](development.md), fetch the golden asset,
and run a smoke scenario:

```sh
python -m tools.verify_model --fetch-only
flapsim run --firmware firmware/splitflap-esp32_n4-v1.4.1-factory.bin \
  --hardware esp32-8-single --scenario scenarios/release-smoke-8.yaml \
  --var FIRMWARE_VERSION=v1.4.1 --known-issues baselines/v1.4.1-golden.yaml \
  --out runs/smoke-8
```

For sixteen modules use `esp32-16-dual` / `release-smoke-16.yaml`. Three homing
runs use `homing-random.yaml`, `esp32-8-single`, distinct output directories and
`--seed 7`, `--seed 19`, `--seed 42`. Every manifest records the seed and actual
starting angles. Each execution has its own flash, process, port and evidence.

For realistic gearbox and module variance, repeat with
`esp32-8-single-realistic` or `esp32-16-dual-realistic`. These presets leave the
firmware's assumed 2048 steps unchanged. `wait_display` additionally requires
each named flap to land within one quarter pitch of its center; `.` ignores
that module's character and alignment. Stopped coils alone do not satisfy this
check. State, timeline and replay expose fractional flap position and signed
landing error. See [hardware-model.md](hardware-model.md) for units, seed
resolution, and the model-policy limitation of the acceptance band. Do not
adjust mechanics or relax the assertion to hide a golden-image landing error;
retain the report and triage it against the physical model and firmware.
An observed matching-but-misaligned landing remains a real failure even when
that step has a golden lost-command timeout allowance; that allowance cannot
turn the landing error into a known-issue pass.
That evidence persists through later nonmatching settlements in the same wait;
an actual matching, aligned settlement can still satisfy the wait normally.

The [calibration scenario](calibration.md) pairs `calibration-8` with
`esp32-8-single-calibration`: independently configured module sensors and flap
zeros, actual offset writes through `POST /settings`, real reboot and persistence
checks, and every drum's fractional alignment. Its fixed fixture isolates
calibration from gearbox-ratio drift; the realistic presets exercise both.

## Version policy

Version matching is strict by default. Supply the intended tag with
`--var FIRMWARE_VERSION=<tag>`; both smoke scenarios assert serial and HTTP
versions. Do not edit expectations to follow whatever the device reports.

There is no version exception. The previous v1.4.0 golden image reported
`v1.4.0-rc.1` (fixed in firmware by
[SFD #87](https://github.com/DrewFerg11/Split-Flap-Display/pull/87)); the v1.4.1
golden image reports its own tag, so the exception was retired.

`python -m tools.verify_negative_version --out runs/negative-versions` proves
that exact-match expectations of the golden tag's prerelease (`v1.4.1-rc.1`, the
v1.4.0 failure mode in reverse) and a deliberately wrong `v9.9.9` both fail with
exit 5 against the pinned image,
produce failing JUnit evidence, and reap QEMU.
An unexpected pass, timeout or different failure makes this verifier fail.

## Known-issue baselines (golden runs only)

The golden image has known firmware defects that the strict smoke scenarios
correctly detect. To keep the emulator's own CI meaningful without hiding them,
golden runs pass `--known-issues baselines/v1.4.1-golden.yaml`. The policy is
tracked in [#15](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/15).

- A baseline names one firmware SHA-256. Any other firmware is rejected before
  QEMU starts. Release gating of new firmware never passes a baseline, so
  strict behaviour is the default.
- `serial` entries are anchored full-line regexes, each linked to an
  `owner/repo#N` issue. Matching lines are tolerated by `assert_no_errors` and
  the final sweep, and counted in the report. Everything else still fails.
- `expected_failures` entries match one scenario name, step number and action,
  plus either an error regex or `timeout` (the step's own deadline only, never
  the run deadline). A match marks that step `xfail`, marks the remaining steps
  `skipped_known_issue`, and the run passes as **passed_with_known_issues**. An entry may
  add `requires_serial`, an anchored regex: it then applies only when some line of the run's
  serial log matches, i.e. when the failure's known cause was observed. The golden baseline
  uses it for the 16-module boot-homing timeout that follows a #14 write timeout
  ([#94](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/94)).
- Expectations are non-strict because the race is intermittent. Entries that
  did not occur are listed as "not observed" in the report.
- Runtime failures, QEMU exits, model stalls and anomalies are never tolerated.

The manifest (`status`, `known_issues`), `report.md`, JUnit (`skipped` with the
issue link) and the Actions job summary all show the baseline prominently.
Remove entries as their issues are fixed, and delete the file when it is empty.

### Retiring the SFD#54 and SFD#86 entries

Split-Flap Display fixed the settings race on its `main` (`ef65640`, SFD#91: one locked
Preferences handle, and unset keys no longer logged). That also removes the fresh-NVS
diagnostic that SFD#86 splices into. Measured natively without any baseline, with seeds
1-3 on each layout:

| Firmware | `release-smoke-8` / `-16`, strict | Fresh-NVS diagnostic lines (smoke-16) |
|---|---|---|
| golden v1.4.1 | 0/4 pass (lost settings writes time out) | 28,363 |
| `ef65640` | 6/6 pass | 0 |

No firmware release contains the fix yet, so the golden image and its baseline stay as
they are. The nightly `firmware-main-strict` job (`ci-nightly.yml`) builds firmware
`main` and runs both release smokes with no baseline. When a release carrying the fix
becomes the golden image and that job is green, remove the SFD#54 and SFD#86 entries,
along with `tools/sfd86_patterns.py` and its tests. The #14 entries stay until the
scheduling artifact itself is fixed.

### Native-only setup and MQTT steps

Native scenarios that declare `modes: [native]` may also prepare the device:

```yaml
modes: [native]
services: [mqtt]                 # a supervised local Mosquitto and an observer subscribed to '#'
clock: {start: "2027-03-28T00:57:40Z", sync: boot}   # fixed wall clock (UTC); sync: boot|station
nvs:                             # seeded settings store: str -> str, int -> i32, float -> blob
  config:
    ssid: flapsim-lab
    password: lab-password
    mqtt_server: ${MQTT_HOST}    # provided by the runner, as is ${MQTT_PORT}
    mqtt_port: ${MQTT_PORT}
```

Seeding NVS starts from a provisioned device without going through `POST /settings`, which
the golden firmware's SFD#54 race would usually lose. `mqtt_publish: {topic, payload, retain?}`
publishes a command, and `wait_mqtt: {topic, payload? | json?, retained?}` waits for the next
matching message after the previous match (`json` is a subset match); every message is kept in
`mqtt.jsonl`. Bundled examples: `mqtt-native-8` (Home Assistant discovery, availability,
inbound `set` → drums, state publishing), `clock-dst-native-8` (Central European Time across the
spring-forward hour) and `date-rollover-native-8` (day, month and year at New Year).

### Native-mode baseline

`baselines/v1.4.1-native.yaml` is the native-mode counterpart for the emulator's own native
CI. Instead of an image checksum it binds `firmware_source_commit`, and applies only to a
**clean** build of that commit (a dirty or different checkout is rejected before launch). A
baseline binds exactly one of `firmware_sha256` (QEMU mode) or `firmware_source_commit`
(native mode) and is refused by the other mode. Native mode runs the loop and web-server tasks
as truly parallel threads, so the SFD#54 lost-write race reproduces far more often than in
QEMU mode, including before the smoke-8 reboot (step 10).

## Schema

```yaml
version: 1
name: example
timeout: 300
modes: [qemu, native]   # optional; default both. Restrict when a step needs one mode.
steps:
  - wait_serial: "AP IP Address: http://192.168.4.1"
    timeout: 120
  - wait_display: {text: XXXXXXXX}
    timeout: 120
  - http:
      path: /version
      status: 200
      json: {version: "${FIRMWARE_VERSION}"}
      wait_ready: true
    timeout: 60
```

Top-level fields are required and unknown fields are rejected. Steps have
exactly one supported action, an optional nonempty `name`, and an optional
positive wall-clock `timeout` (default 30 seconds). The total scenario timeout
also applies; `--timeout` overrides only that total. Each action gets the smaller
remaining limit. Invalid syntax, unsupported actions, duplicate YAML keys,
invalid expectations and missing variables fail before QEMU starts.

`--var KEY=value` accepts a JSON value when valid, otherwise a literal string.
A whole YAML value `${KEY}` preserves its JSON type, including booleans, arrays
and objects. Embedded variables such as `Firmware: ${FIRMWARE_VERSION} (` must
be strings. Variable names are unique identifiers; mapping keys stay literal.
Missing/malformed placeholders, nonfinite numbers and non-JSON YAML objects
are rejected. There is no shell evaluation.

| Action | Value and behavior |
|---|---|
| `wait_serial` | Literal nonempty string, `{contains: text}`, or `{regex: expression}`; wait for a new match at/after the serial cursor and advance the cursor past it. Regex uses multiline matching and must consume characters. |
| `expect_serial` | Same matcher; search all observed serial history immediately without advancing the cursor. Missing evidence is an assertion failure. |
| `wait_display` | `{text: " HELLO  "}`; exact module-order text, with `.` as a one-module wildcard. Spaces are significant. Requires a fresh all-module settlement. Optional `history` and `allow_unchanged` are described below. |
| `http` | Required `path` and integer `status`; optional `method` (`GET`/`POST`), JSON `body` for POST, `json` response expectation, and GET-only `wait_ready`. Paths target the run's loopback AP endpoint. Redirects are returned as their own status. Bodies are limited to 1 MiB. |
| `assert_no_errors` | `{}` rejects new firmware error/panic/abort/exception lines and nonzero cumulative model stalls/anomalies. Its explicit narrow policy options are described below. |
| `sleep_guest` | Positive seconds of model guest time. Wall time cannot satisfy it; the step/total wall deadline still applies. A backwards guest clock fails. |

HTTP JSON expectations are recursive subsets of objects. Arrays match exact
length/order and scalar types are exact (`true` does not equal `1`). Invalid
JSON and status/value mismatches fail. GET readiness retries transport failures;
POST requests are sent once. A successful POST is an HTTP result, not proof of
physical movement. The entire network exchange is deadline-supervised, including
slow/trickled headers and bodies, early emulator exit and CLI cancellation.

## New observations and command completion

The serial cursor starts at zero. A successful `wait_serial` consumes its match;
`expect_serial` can still inspect older evidence. A mutating HTTP request first
sets the serial cursor to the current end, so a reboot wait cannot reuse an old
boot/AP line. The smoke scenario requires the real firmware marker
`Reset reason: software reset` and then a new AP marker. Repeated AP output alone
does not establish a reboot.

Before POST, the executor records the current movement ID and any valid settled
state. A following display wait requires a newer movement and a complete
all-module settlement. The reader checks activity/state/activity identity;
cached state during movement, a partial second row or HTTP 200 cannot pass.
Commands should start from an idle display when requesting observable movement.
Successive display observations also advance a snapshot cursor.

For a deliberately idempotent request, `{text: ..., allow_unchanged: true}` may
accept the same pre-command settlement only when the display was already
settled with exactly that physical text. The report marks `unchanged: true`.
This option requires a preceding POST and is opt-in per wait. The sixteen-module
smoke repeats the exact same dual-row command to exercise it.

`history: true` reads complete `move_finished` timeline records in order and
validates their full module schema. This captures the brief intermediate blank
homing settlement even if the current display has already moved to startup X.
It excludes initialization (movement ID zero), old consumed snapshots and any
pre-command movement. Historical and idempotent modes cannot be combined.
The homing scenario records blank first, asserts diagnostics, then waits for the
later current X settlement. Final runtime success still validates complete
model files and a current all-off settlement after stopping QEMU.

`assert_no_errors` has its own serial cursor, independent of ordinary waits.
It examines complete new lines and retains an unfinished line for the next
check. `fresh_nvs: true` permits only the exact known Preferences.cpp:50
`nvs_open failed: NOT_FOUND` diagnostic. A reset can interrupt either its source
header or payload: an observed prefix of that exact diagnostic immediately
followed by the exact ESP32 ROM banner (`ets Jul 29 2019 12:21:46`) is also
recognized as an interrupted record. Raw serial evidence is unchanged; unknown
characters, other sources, suffixes and errors still fail.
`default_missing_bus1: true` requires
exactly one code-2 write failure for each absent default address 32–39, and is
valid only for the eight physical bus-0 modules. It is used before single-bus
configuration; later checks reject all such missing-module writes.
`faulted_modules: [0x25]` permits any number of code-2 write failures for the
listed modules. Each must be configured with an injected I2C fault (see
[hardware-model.md](hardware-model.md#injected-faults)). The firmware numbers
modules by address on both buses, so this cannot be combined with
`default_missing_bus1`. No policy allows unrelated errors. A final check catches complete new errors after the
last explicit assertion and before shutdown.
When a scenario has no `assert_no_errors` step, that final check scans all
complete serial lines with no allowances.

## Shipped scenarios

The firmware API was checked at published tag v1.4.0:
`GET /version` returns `{version}`; `POST /settings` changes `wire1Addresses` and
`wire1Offsets` and triggers software reset; `POST /text` accepts
`{mode: single, words: [HELLO], center: true}` or
`{mode: dual-single, row1: HELLO, row2: WORLD, center: true}`.
The tests assert physical text instead of deriving it from firmware state.

`release-smoke-8.yaml` checks AP/initial X, serial+HTTP version, configures a
single bus, confirms a new software reboot, waits for `WIFI ERR`, reads persisted
settings, sends HELLO, and verifies exact centered ` HELLO  `.
`release-smoke-16.yaml` checks initial X, versions, then independent centered
rows ` HELLO  ` / ` WORLD  `, followed by the explicit idempotent repeat.
`homing-random.yaml` asserts the intermediate all-blank settlement and zero
stalls/anomalies before completing startup X. PRs run seed 7 and the nightly workflow runs seeds 19 and 42.
`settings-race-8.yaml` stresses the settings store
([SFD#54](https://github.com/DrewFerg11/Split-Flap-Display/issues/54), #24) with the
`settings_stress` step: each of `rounds` (1–500) sends one `POST /settings` with a new
`name` (`stress-<round>`) while `readers` (0–16, default 4) `GET /settings` run at the same
time, then reads the name back. A concurrent read that sees anything but the previous or
the new name is a raced read; a read-back without the new name is a lost write. Either
fails the step with the round and the values seen. v1.4.1 loses the first write in native
mode, so golden runs record it as a #54 expected failure; firmware with the fix passes
all 30 rounds.
`clock-soak-native-8.yaml` (native only) runs time mode for three guest hours, 180 display
updates, and then asserts no errors and no model faults. Run it with `--speed 50`: it takes
about 3.5 wall minutes. The nightly workflow runs it (and `settings-race-8`) on the golden source under the
native baseline; PR checks skip both to stay fast.
`first-boot-8.yaml` / `first-boot-16.yaml` follow a new user from a blank flash
([#22](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/22)): access point and
no-Wi-Fi display, the compiled defaults in `GET /settings`, one save of layout (8 only),
a calibration offset, name and MQTT broker through the access-point web UI, the reboot
with every saved value read back and the new layout in effect, then
`POST /settings/reset` back to the first-boot display and defaults. After the reboot
they wait for `Homing` before the display check; after the reset, which does not home,
they wait for the blank settlement (`history: true`). Either way a display that already
showed the target can't match early. CI runs both natively against the golden source
under the native baseline, where v1.4.1 fails them on
[SFD#54](https://github.com/DrewFerg11/Split-Flap-Display/issues/54)'s lost writes;
firmware with that fixed passes them strictly.

## Evidence and validation

Every claimed run directory receives `run.json`, `report.md`, `junit.xml` and
`replay.html`, including assertion failure, timeout, signal and early QEMU exit.
The report lists every step as passed/failed/not-run, expected and actual data,
wall and guest timings, counters, input/build/hardware provenance and the last
50 serial lines. JUnit records skipped steps and a separate lifecycle failure
when execution never starts. Missing serial/model data is shown as unavailable.
When `GITHUB_STEP_SUMMARY` is set the report is appended to it.

The self-contained replay has play/pause and snapshot scrubbing, physical bus
rows, coil activity, timestamps and counters. Complete snapshots remain useful
on failure; absent/partial/malformed timelines carry a visible warning. Embedded
JSON cannot close a script tag, text is rendered through DOM textContent, and
JUnit XML controls are sanitized. No browser, external assets or GIF tool is
required. Replay playback advances snapshots at 100 ms per frame; guest times
are displayed explicitly rather than inferred from that playback speed.

Fast tests cover validation/substitution, cursor freshness, stale/partial-row
state, idempotence, HTTP status/typed JSON/trickle deadlines, guest-time sleep,
narrow error policies and report/replay safety. A minimal real-run fixture in
`tests/fixtures/golden-8-evidence.json` retains its firmware/build provenance and
the actual blank/X settlements. A local real-firmware strict version negative
passed in 53.148s with QEMU exit 0 and unchanged source input.

CI runs six real-firmware jobs: two smoke layouts, three homing seeds, and an
independent `negative-versions` job asserting strict/wrong-version rejection.
The negative checks run even when a smoke job fails. Each
scenario has a 300-second bound; logs, manifests, reports, JUnit and replays are
uploaded on failure as well as success, excluding firmware/private flash.
The [pre-rebase CI](https://github.com/DrewFerg11/Split-Flap-Emulator/actions/runs/39460294879)
passed fast checks and all three homing seeds. The strict eight-module smoke
failed on rebooted Preferences errors in 94.426s; the sixteen-module smoke
failed on HTTP 200 without motion in 222.304s. These are failed-run durations,
not successful smoke timing acceptance. Fresh checks repeat after rebasing.
The preceding runner CI passed both real CLI boots within 240 seconds; its
local eight-module run took 599.849s on the slower shared host. Do not substitute
local-host timing for CI measurements.

## Golden acceptance blockers

The checksum-pinned published v1.4.1 image (like v1.4.0) does not pass both complete smokes.
[Firmware #54](https://github.com/DrewFerg11/Split-Flap-Display/issues/54)
records settings GET readback returning defaults despite confirmed persistence,
and dual-row text POST returning HTTP 200 without subsequent motion. Readback
sometimes passes, while rebooted Preferences missing-key errors still fail the
strict error gate. The maintainer owns the firmware fix. The accepted #15 golden baseline now
records these failures explicitly so they do not block emulator PRs.

The intermittent I2C read Error 263 and write `error code: 5` in
[emulator #14](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/14)
are an emulator scheduling artifact, not a firmware or model defect. QEMU runs the
ESP32's two cores in turn on one thread. ESP-IDF's I2C ISR for `Wire1` (core 1) can be
parked between posting DONE to the `Wire1` task (core 0) and setting the driver idle;
the task's next transfer then has its interrupt ignored and times out. A write that
times out loses one coil pattern, which can stall one drum. The golden baseline
records both lines, and each logged write timeout (`explains_stalls: 1`) excuses at
most one drum stall in the same run; anomalies, other write codes and stalls beyond
that count still fail. v1.4.1 logs only a module's first failed write per boot
(`SplitFlapModule::hasErrored`), so later timeouts on that module are silent. Once
the run has logged one, the stalls the model counts in `lost_write_stalls` (each
one followed a write that delivered only its address) are explained too. Scenario reports list tolerated stalls under the known issues.
A faithful fix needs different core scheduling, tracked in #14.

The checksum-bound golden baseline also records the exact Preferences.begin
NOT_FOUND timestamp fragment documented in [SFD #86](https://github.com/DrewFerg11/Split-Flap-Display/issues/86).
Its retained raw UART capture shows request logging inserted inside a diagnostic
record. Where the split lands varies between runs: the line ends with the
diagnostic's tail starting anywhere in its `[ timestamp]` (a bare digit suffix or
the full timestamp), preceded by the other task's partial output. That output must
be the `POST /text` or `/settings` request log, or that request's body in the
shapes v1.4.1 logs (`{"mode":"<mode>",...}` for `/text`, settings keys for
`/settings`, scalar values only), followed by the exact Preferences.cpp:50
message. Other text, such as `{not-json}`, still fails. The splice regexes are
generated by [`tools/sfd86_patterns.py`](../tools/sfd86_patterns.py), which lists
the accepted shapes. An unspliced diagnostic line is
left to the normal `fresh_nvs` policy. Counts/first occurrence remain visible. Strict default and new-release
runs still reject it, as do other malformed messages. This records current-image
behavior while the firmware maintainer investigates writer/SDK locking; it does
not repair logging or establish physical-hardware behavior.

## Declarative runtime controls

Scenarios can mutate the running emulator directly through the same typed,
serialized dispatcher and durable `control.jsonl` audit as the HTTP API. A
scenario containing these actions automatically provisions a private dispatcher
and model channel. It requires no HTTP dependency, listener or token file. Add
`--control-port 0` and install the `control` extra to expose the authenticated
loopback API as well. No token, HTTP self-request or shell command
belongs in the YAML. Normal scenarios without controls retain their existing
startup behavior.

| Action | Value |
|---|---|
| `set_mechanics` | `{id: 1, expected_revision: 0, bus: 0, addr: 32, patch: {sensor_window: 42}}` |
| `set_fault` | `{id: 2, expected_revision: 1, bus: 0, addr: 32, faults: {i2c: missing}}`; canonical deterministic sensor/motor/I2C modes only |
| `reset` | `{id: 1}`; success requires the observed controller generation receipt |
| `set_power` | `{id: 1, on: false}` or `{id: 2, on: true}` |
| `power_cycle` | `{off_id: 1, on_id: 2, off_seconds: 0.05}`; two audited transitions, optional host-time off interval 0–30 seconds |
| `set_speed` | `{id: 1, speed: 10}`; native only, 0.25–50; QEMU scenarios reject this action before launch |

Commands use the API's explicit IDs and mechanics compare-and-set revisions.
IDs increase in each API namespace: mechanics/faults share one namespace;
reset, power and speed each have their own. The power-on ID in a cycle must
exceed its off ID. Applied edits, reset/power receipts and clock controls obey
the same persistence and fidelity limits described in [the API contract](control-api.md).
Foreign concurrent mutations can cause a conflict; the scenario does not hide
it by rewriting revisions or IDs.

Only explicit `model_busy`/`backend_busy` rejections that applied nothing retry,
using the identical command until the step/run deadline. A transport/audit
failure, unknown receipt, unavailable backend or revision/ID conflict stops the
scenario. An uncertain mutation is never retried automatically. Each step
records receipts and busy-rejection counts; a partial power cycle preserves the
off receipt if the next transition fails. Cancellation/deadline while off stops
without issuing an unrequested power-on; the runner still owns cleanup.

Runtime controls establish the same pre-command serial/movement baseline as
mutating firmware HTTP steps. Subsequent waits must observe fresh events and
settlements. A receipt proves control application, not final physical landing;
follow it with `wait_display` and normal error assertions. For an injected I2C
fault, `assert_no_errors: {faulted_modules: [37]}` may excuse only the existing
specific module-37 write-error diagnostic after a preceding valid I2C injection.
It does not excuse other errors, model anomalies or unexplained stalls. Repair
does not erase historical serial evidence.

`runtime-controls-16` demonstrates healthy boot, a geometry edit, live module
disconnection with observed firmware failure, repair, controller reset and power
cycle on the default sixteen-module hardware, without firmware settings writes.
`runtime-speed-native-16` demonstrates native rate changes with reset/power.
Both are installed resources:

```sh
flapsim run --mode native --native-build build/native/golden \
  --hardware esp32-16-dual --scenario runtime-controls-16 \
  --known-issues baselines/v1.4.1-native.yaml --out runs/runtime-controls
```
