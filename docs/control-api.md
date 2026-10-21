# Live state API (issue #20, first implementation slice)

The runner can expose current physical state independently of the firmware's
web server. This slice implements authenticated `GET /api/emu/state` in QEMU
mode. Mechanics/fault mutation, reset/power, speed, I2C history and WebSockets
remain unavailable and are explicitly false in `capabilities`. Issue #20 stays
open. See [the implementation plan](control-api-plan.md) for later slices.

```sh
python -m pip install -e '.[control]'
flapsim run --firmware firmware/splitflap-esp32_n4-v1.4.1-factory.bin \
  --hardware esp32-8-single --scenario homing-random \
  --var FIRMWARE_VERSION=v1.4.1 --control-port 0 --out runs/live
```

`--control-port 0` binds an OS-assigned port on `127.0.0.1`; an explicit port is
also allowed. Without this option no service/socket is created and the optional
HTTP dependencies are not imported. The Docker image includes the extra.
The listener is inside the container's loopback namespace; external binding and
live-panel/serve mode are future work, not provided by port publication alone.

While the run is executing, read `control.endpoint` from `run.json`. The existing
`endpoint` still points to the firmware web server. Read the bearer token from
`control-token` in the output directory (created with permissions 0600), and send
`Authorization: Bearer <token>` to the control endpoint. Token contents are never
recorded in the manifest, logs or verification evidence. Treat that file as a
secret; omit it from uploaded artifacts. Foreign Origin/Host headers are rejected,
and responses prohibit caching. No permissive CORS or external bind is offered.

The API lives only as long as the bounded scenario. After success, failure,
timeout or cancellation the listener is closed; final metadata records
`control.status: stopped`. Use a scenario with sufficient runtime for inspection.
This is not an always-on server. Backend startup/failure returns 503, missing or
incorrect authentication 401, and foreign Origin/Host 403. Unsupported routes
are not installed (404); a wrong method on the state route returns 405.

Successful responses include `api_version: 1`, a run ID, backend, boot generation,
capabilities, `live: true`, and `state`. The state contains guest time, movement
and settlement IDs, counters, rows/text and every module's current physical angle,
flap position/error, char, latch, coils, phase, electrical hall level, energized
flag and resolved mechanics. `boot_generation` counts model guest-reset callbacks
since this process initialized its model; the initial boot is generation zero.

These are owner-locked live snapshots, including during movement, not stale
settlement-file reads. Querying does not advance the clock or append timeline
records, modify counters, or manufacture a settlement. A paused QEMU still serves
snapshots; guest time/physical state remain frozen. A snapshot can describe a
transaction whose buffered bytes have not yet committed to the expander latch.
It always reports committed physical state coherently.

## Private backend interface and native integration

The runner inherits a private Linux `AF_UNIX/SOCK_SEQPACKET` connection into the
child via `FLAPSIM_CONTROL_FD`. The only v1 request is the exact ASCII packet
`FLAPSIM_STATE/1`; the reply is one JSON snapshot packet, bounded to 64 KiB by
the client. Malformed/oversized requests receive an error. The adapter never
blocks on a slow consumer; transport failure disables that connection without
stopping firmware. Only one request is in flight, with a 500 ms host deadline;
a timeout closes the channel so a late reply cannot satisfy another request.
This narrow packet protocol replaces the plan's tentative framed-stream proposal
for this Linux-only, read-only slice. It leaves control/telemetry separation for
the mutation and streaming work.

QEMU registers its read callback with the pinned `qemu_set_fd_handler`, confirms
the BQL is held and reads guest time from `QEMU_CLOCK_VIRTUAL`. The callback runs
even while paused. No QEMU source patch or background model reader is used.
The C serializer/transport are backend-independent and compile into the native
model library introduced by merged #27. The still-open native runner stack
(#30/#31/#32 at implementation time) must call them while holding its existing
`host::model_lock()`, preserve its inherited descriptor over re-exec, and restore
boot-generation accounting. This PR does not claim native HTTP integration;
that follows on the reconciled native runner rather than duplicating it here.

## Verification

```sh
python -m tools.verify_model --fetch-only
python -m tools.verify_control \
  --firmware firmware/splitflap-esp32_n4-v1.4.1-factory.bin \
  --hardware esp32-8-single --out runs/control-probe
```

Repeat with `esp32-16-dual`. CI runs both probes. The probe requires motion,
reads repeatedly while QMP-paused without changing state, resumes to centered X,
hard-resets through private QMP while paused, verifies exactly one reset with
unchanged external state, then requires a new centered X settlement with zero
stalls/anomalies. Private QMP use is verification, not a public reset capability.
It retains `control-verification.json` alongside standard run evidence and
verifies normal input-integrity/process cleanup through the runner. It does not
validate all serial diagnostics or claim the firmware's known bugs are repaired;
the existing golden scenarios keep their independent serial/version assertions.

Initial local verification against the checksum-pinned v1.4.1 image passed on both
layouts: 100.971 seconds for eight modules and 127.450 seconds for sixteen.
Both preserved external state across the private reset and ended centered with
zero stalls/anomalies. After rebasing onto merged native #27 (`7fbe3b6`), all
391 native/Python regression tests passed, together
with Python lint/format, C formatting and diff checks. Exact-image and hosted
verification run in CI; these local runs do not claim the image was tested here.
The pinned v1.4.1 native source also builds with the shared serializer/transport
in its model library, passes shim tests, and passes boot and Improv re-exec
verification on both layouts. This checks model compatibility; it does not add
the native HTTP transport that depends on the remaining native runner stack.
