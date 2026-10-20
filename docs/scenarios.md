# Firmware scenarios and evidence

Build/install using [development.md](development.md), fetch the golden asset,
and run a smoke scenario:

```sh
python -m tools.verify_model --fetch-only
flapsim run --firmware firmware/splitflap-esp32_n4-v1.4.0-factory.bin \
  --hardware esp32-8-single --scenario scenarios/release-smoke-8.yaml \
  --var FIRMWARE_VERSION=v1.4.0 --golden-version-exception --out runs/smoke-8
```

For sixteen modules use `esp32-16-dual` / `release-smoke-16.yaml`. Three homing
runs use `homing-random.yaml`, `esp32-8-single`, distinct output directories and
`--seed 7`, `--seed 19`, `--seed 42`. Every manifest records the seed and actual
starting angles. Each execution has its own flash, process, port and evidence.

## Version policy

Version matching is strict by default. Supply the intended tag with
`--var FIRMWARE_VERSION=<tag>`; both smoke scenarios assert serial and HTTP
versions. Do not edit expectations to follow whatever the device reports.

The published v1.4.0 factory image reports **v1.4.0-rc.1**. The explicit
`--golden-version-exception` is accepted only when the source SHA-256 is
`a087801e2ba5e08154af28a636c310b988b43417f28c7ddb7bae4f3df9f6221e`
and the requested `FIRMWARE_VERSION` is `v1.4.0`. It changes that variable's
effective expectation to `v1.4.0-rc.1`; other checks stay enabled. The manifest,
Markdown report and Actions summary prominently record the exception, original
tag, effective version and checksum. Arbitrary releases or other expected
versions cannot use this exception. A matching checksum alone never enables it.

`python -m tools.verify_negative_version --out runs/negative-versions` proves
that strict `v1.4.0` and deliberately wrong `v9.9.9` expectations both fail with
exit 5 against the pinned image, produce failing JUnit evidence, and reap QEMU.
An unexpected pass, timeout or different failure makes this verifier fail.

## Schema

```yaml
version: 1
name: example
timeout: 300
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
| `assert_no_errors` | `{}` rejects new firmware error/panic/abort/exception lines and nonzero cumulative model stalls/anomalies. Two explicit narrow policy flags are described below. |
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
`nvs_open failed: NOT_FOUND` diagnostic. `default_missing_bus1: true` requires
exactly one code-2 write failure for each absent default address 32–39, and is
valid only for the eight physical bus-0 modules. It is used before single-bus
configuration; later checks reject all such missing-module writes. No policy
allows unrelated errors. A final check catches complete new errors after the
last explicit assertion and before shutdown.

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
stalls/anomalies before completing startup X. CI runs three recorded seeds.

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

CI runs five real-firmware jobs (two smoke layouts and three homing seeds),
plus asserted strict/wrong-version negatives in the eight-module job. Each
scenario has a 300-second bound; logs, manifests, reports, JUnit and replays are
uploaded on failure as well as success, excluding firmware/private flash.
Current-head real scenario results and measured CI duration remain pending
until the draft PR's workflow finishes. The preceding runner CI passed both
real CLI boots within 240 seconds; its local eight-module run took 599.849s on
the slower shared host. Do not substitute local-host timing for CI measurements.
