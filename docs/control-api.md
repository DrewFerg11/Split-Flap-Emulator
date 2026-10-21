# Live state and mechanics API (issue #20)

The runner can expose current physical state independently of the firmware's
web server. The implemented QEMU routes provide authenticated live state, atomic mechanics
edits and lookup of the latest applied edit. Fault mutation, reset/power, speed,
I2C history and WebSockets remain unavailable and are explicitly false in `capabilities`. Issue #20 stays
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

## Atomic mechanics edits

`POST /api/emu/modules/{bus}/{addr}/mech` uses decimal bus/address (for example,
`0/32`). Send `Content-Type: application/json` and the same bearer token:

```json
{"id":1,"expected_revision":0,"patch":{"sensor_window":25,"sensor_polarity":"active-low"}}
```

Supported patch fields are `steps_per_rotation`, `magnet_position`, `flap_offset`,
`sensor_window` and `sensor_polarity`. Values are resolved literals, with the same
limits as hardware configuration. Unknown fields, random specifications, empty
patches, booleans in integer fields, non-finite numbers and duplicate JSON keys
are rejected. Bodies are UTF-8 JSON, limited to 2048 bytes and one second to arrive.
Topology, flap order, physical angle, firmware settings and faults are not editable.

All edits require **every motor off and both model buses idle**. Otherwise the model
returns `409 model_busy` without applying or queuing anything; retry when idle.
The complete resulting geometry must be valid, including inherited fields and
the current angle. Changing rotation size never wraps/rescales the angle.
An accepted no-op still creates a revision and a receipt.

`expected_revision` is a compare-and-set guard. Before the first edit it is zero;
subsequent snapshots expose `state.mechanics_revision`. A mismatch returns
`409 revision_conflict`. `id` is a client-selected integer in `1..2^53-1`, strictly
increasing across successfully applied commands for this run. Multiple clients
must coordinate IDs and revisions. Retrying the latest successful ID with the
identical command returns the original receipt without applying it again.
Reusing it with a different command, or using an older ID, returns
`409 stale_or_reused_id`. Rejected commands do not consume their ID.

A `202` response includes `status: applied` and `operation` with the original
command, resulting revision, model sequence and guest time. This implementation
waits for the bounded owner-context application before acknowledging; it has no
asynchronous admission queue. `GET /api/emu/operations/{id}` returns that same
receipt for the **latest applied command only**, even after a controller reset.
Older/unseen IDs return `404` with `status: unknown`; this does not mean they
were never applied. The last command is retained for the model lifetime and
through native model resume. Older IDs remain below the durable high-water mark
and cannot be reapplied. The result endpoint consults the live model, not the
runner's journal.

The runner appends and fsyncs an intent in `control.jsonl` before dispatch, then a
result before acknowledging. The model writes a correlated `mechanics_changed`
timeline/state record and fsyncs the timeline before success. Transport, deadline or audit-write failure
returns `503 result_unknown`, closes the private channel, and never retries the
command automatically. The edit may have applied: consult the model receipt if
the backend is available, or the complete timeline record. An intent alone is not
proof of application. A second concurrent backend request returns bounded 503;
there is no waiting queue. HTTP validation failures never reach the model.

Other mutation errors: malformed JSON 400, foreign Host/Origin 403, missing module
404, body timeout 408, oversized body 413, wrong Content-Type 415 and invalid command
or resulting mechanics 422. Authentication and no-store rules also apply to edits
and operation lookup.

## Evidence extension and resume

The immutable `model.json` and manifest hardware remain the initial configuration.
After the first successful edit, model state, activity and timeline records carry
an additive `mechanics_version: 1` extension within existing `schema_version: 1`:
`mechanics_revision`, `initial_mechanics`, `initial_flaps`, `guest_resets` and
`last_operation`.
Complete snapshots also carry effective `mechanics` and `coils` per module.
Legacy v1 recordings without the extension keep their initial-configuration
interpretation; output with no edits retains its existing format.

Readers validate each record against its own effective geometry, check initial
mechanics against immutable hardware, and correlate the last command with its
resolved result. Settlement freshness includes mechanics revision so an older
settlement cannot masquerade as current after an edit. Replay retains each frame's
geometry and shows revisions/receipts. Native resume validates the version and
initial geometry, restores the overlay **before** validating dynamic angles, and
restores the command high-water mark and reset generation. Bad overlays fail closed.
The edit survives controller reset; it does not recalibrate the firmware or move
the drum to manufacture alignment.

## Private backend interface and native integration

The runner inherits a private Linux `AF_UNIX/SOCK_SEQPACKET` connection into the
child via `FLAPSIM_CONTROL_FD`. The state request is the exact ASCII packet
`FLAPSIM_STATE/1`. A typed edit uses `FLAPSIM_MECH/1 ` followed by a JSON command
containing `id`, `expected_revision`, `bus`, `addr` and `patch`. Requests are limited
to 2048 bytes; the reply is one JSON snapshot or error packet, limited to 64 KiB
by the client. The private dispatcher validates the resulting geometry independently. Malformed/oversized requests receive an error. The adapter never
blocks on a slow consumer; transport failure disables that connection without
stopping firmware. Only one request is in flight, with a 500 ms host deadline;
a timeout closes the channel so a late reply cannot satisfy another request.
This narrow packet protocol replaces the plan's tentative framed-stream proposal
for this Linux-only implementation. Streaming must use a separate channel so
telemetry cannot starve command replies.

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
waits for a centered X settlement, reads repeatedly while QMP-paused without
changing state, toggles electrical sensor polarity without moving a drum, proves
command deduplication, hard-resets through private QMP while paused and verifies
exactly one reset with unchanged external state and preserved mechanics receipt.
It restores the original polarity before resuming and requires a new centered X settlement with zero
stalls/anomalies. Private QMP use is verification, not a public reset capability.
It incrementally retains `control-verification.json` alongside standard run evidence
(including completed stages when a later assertion fails) and
verifies normal input-integrity/process cleanup through the runner. It does not
validate all serial diagnostics or claim the firmware's known bugs are repaired;
the existing golden scenarios keep their independent serial/version assertions.

An earlier diagnostic that paused during motion on the rebased sixteen-module
layout failed strictly on an I2C write timeout (module 33, code 5) and one model
stall. Its artifacts remain in `runs/issue20-rebased-control-16`; no allowance
was added. This does not establish that pausing caused the timeout. The API
probe separates motion observation from paused-guest service checks, and pauses
only after the first centered settlement. Any timeout or model fault still
fails. Arbitrary mid-motion pause/power fidelity needs investigation before
exposing those controls; the current API does not promise it.

Initial local verification against the checksum-pinned v1.4.1 image passed on both
layouts: 100.971 seconds for eight modules and 127.450 seconds for sixteen.
Both preserved external state across the private reset and ended centered with
zero stalls/anomalies. After rebasing onto merged native #27 (`7fbe3b6`), all
392 native/Python regression tests passed, together
with Python lint/format, C formatting and diff checks. Exact-image and hosted
verification run in CI; these local runs do not claim the image was tested here.
The pinned v1.4.1 native source also builds with the shared serializer/transport
in its model library, passes shim tests, and passes boot and Improv re-exec
verification on both layouts. This checks model compatibility; it does not add
the native HTTP transport that depends on the remaining native runner stack.

For the mechanics slice, the complete regression suite passes **453 tests**.
The 162 model/control tests also pass with AddressSanitizer and UndefinedBehaviorSanitizer
instrumentation. The native golden source builds and passes shim tests; boot and
Improv reboot verification pass on both layouts, including a fresh reboot check
with the final model. Direct C/HTTP tests cover atomic rejection, open transactions,
physical I2C hall input, revision conflicts, lost acknowledgements, durable command
deduplication, malformed overlays and restoring angles larger than the initial
rotation. The extended QEMU probes exercise edits and deduplication while paused,
then verify persistence through a real controller reset before restoring geometry
and checking strict centered landing. Hosted CI runs these on both layouts along
with the unchanged golden scenarios and runtime image checks.
