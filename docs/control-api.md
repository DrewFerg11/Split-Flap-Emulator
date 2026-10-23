# Live state, mechanics and reset API (issue #20)

> **Experimental in 1.0.** The control API is shipped for early use and may change in 1.x releases while it is finished.

The runner can expose current physical state independently of the firmware's
web server. The implemented QEMU and native routes provide authenticated live state, atomic mechanics
edits, controller reset/power transitions, native runtime speed, deterministic module faults and operation receipts. I2C history and WebSockets remain unavailable and are explicitly false in `capabilities`. Issue #20 stays
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
a long-running serve mode is future work, not provided by port publication alone.
For a read-only live view of a run, see [the panel](panel.md).

Native mode uses the same option and API contract:

```sh
flapsim run --mode native --native-build build/native/golden \
  --hardware esp32-8-single --scenario boot --control-port 0 --out runs/native-control
```

Rebuild native firmware binaries against the current shims before enabling control.
Native responses identify `backend: native`. A private inherited socket and a
host thread serve requests under the existing model mutex. The channel, run ID,
token, mechanics overlay and latest receipt survive firmware software resets
(re-exec of the same process). Reset generations persist even before the first
mechanics edit. Requests during reset still have the usual bounded deadline;
a timeout leaves the result unknown and poisons the channel as described below.
Public controller reset is available in both modes. Controller power uses the approximation described below. Native supports runtime speed controls; QEMU timing remains fixed at startup.

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

Successful state and mechanics responses include `api_version: 1`, a run ID, backend, boot generation,
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
Topology, flap order, physical angle and firmware settings are not editable.
The deterministic fault subset below is editable through a nested `faults` patch.

Geometry and mixed geometry/fault edits require **every motor off and both model buses idle**. Otherwise the model
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
command, resulting revision, model sequence and guest time. The runner verifies
the entire command identity before returning 202, including for a deduplicated
retry: `operation.command.id` always matches the submitted ID. This implementation
waits for the bounded owner-context application before acknowledging; it has no
asynchronous admission queue. `GET /api/emu/operations/{id}` returns that same
receipt for the **latest applied command only**, even after a controller reset.
Older/unseen IDs return `404` with `status: unknown`; this does not mean they
were never applied. The last command is retained for the model lifetime and
through native model resume. Older IDs remain below the durable high-water mark
and cannot be reapplied. The result endpoint consults the live model, not the
runner's journal.

The runner appends and fsyncs an intent in `control.jsonl` before every admitted
edit dispatch, then a result before acknowledging, including deduplication hits
and model rejections. The model writes a correlated `mechanics_changed`
timeline/state record and fsyncs the timeline before success. Transport, deadline
or audit-write failure returns `503 result_unknown`, closes the private channel,
and never retries the command automatically. A best-effort `unknown` journal
entry identifies the command, failed stage, exception class and channel closure; if the audit sink
itself is broken, that entry may also be absent. An intent without a result is
unknown, and neither an intent nor an `unknown` entry proves application failed.

Channel poisoning makes **all control reads and edits unavailable for the current
child**, including state and operation lookup. The current runner does not
reconnect a poisoned channel to a running child, including after native re-exec. Firmware execution continues;
control recovery requires a new child/run. Reconnection is future integration work.

The latest receipt endpoint is usable only while the backend channel is available
and that edit remains the latest applied command. A subsequent edit replaces that
live receipt. The **append-only timeline retains every completed
`mechanics_changed` record**, so the earlier command's receipt remains recoverable
there even after a later edit. Preserve the run's timeline and audit journal.
Use `control.run_id` in `run.json` to identify the timeline's run and match it
with the journal's run ID and the receipt's command ID; IDs are scoped to a run.
An acknowledged edit or deduplication also has a per-attempt journal result. Do
not interpret an older ID's `404 unknown`, or an intent without a complete model
record, as proof that the edit did not happen.

A second concurrent backend request returns bounded 503; there is no waiting
queue. HTTP validation failures never reach the model.

Other mutation errors: malformed JSON 400, foreign Host/Origin 403, missing module
404, body timeout 408, oversized body 413, wrong Content-Type 415 and invalid command
or resulting mechanics 422. Authentication and no-store rules also apply to edits
and operation lookup.

## Controller reset

`POST /api/emu/reset` accepts only `{"id":1}` with the same bearer, Host/Origin,
JSON, 2 KiB body and one-second upload rules as mechanics edits. Reset IDs are
strictly increasing integers in `1..2^53-1`, in a **separate namespace** from
mechanics IDs. The runner serializes reset with model reads and mechanics edits;
a concurrent request returns `503 backend_busy` before admission and consumes no ID.

QEMU performs one QMP `system_reset` and reports `fidelity: qmp-hard-reset`.
An executing guest remains running; a paused guest remains stopped (the pinned
QEMU labels it `prelaunch` after reset, rather than `paused`). The API never
resumes a stopped guest merely to restore its run-state label. It does not change
timing or simulate removing controller/module power. Native uses the existing same-PID
software reset/re-exec path and reports `fidelity: native-software-reexec`, with
`ESP_RST_SW` in firmware. Neither backend resets external drum angle, expander
latches or model counters; mechanics overlays/receipts and persistent firmware
settings remain. Firmware may naturally move drums as its new boot proceeds.

A `202` response means the runner **observed exactly one new model reset
generation**, not merely that QMP or the native thread accepted the request.
It contains `api_version`, `run_id`, `backend`, `command`, `status: applied`,
`before_generation`, `boot_generation`, `sequence`, `guest_ns` and `fidelity`.
This is an observation of controller reset, not a promise that firmware setup,
networking or homing has completed. Wait for the desired state separately.
Native reset is invoked only after releasing the model mutex; the existing
re-exec checkpoint preserves moving state and inherited listeners/channels.

Retrying the latest admitted ID returns its original receipt without dispatching
another reset, including after native re-exec. Older IDs return
`409 stale_or_reused_id`. Only the latest reset receipt is retained in memory;
`GET /api/emu/reset/operations/{id}` returns it independently of the model channel.
An absent/older ID returns `404 status: unknown`, which never proves it was not
applied. These receipts last for the runner's run, not across runner restart.
They do not replace mechanics receipts at `/api/emu/operations/{id}`.

Before dispatch the runner fsyncs an intent in `control.jsonl` with `kind: reset`;
a result is fsynced before acknowledging the observed generation. Replays return
the already recorded receipt. A lost reply, rejected backend reset, audit failure,
observation deadline or unexpected generation jump returns `503 result_unknown`,
consumes that ID and closes the model channel. It never automatically retries.
The latest receipt remains queryable with `status: unknown`, and a repeated ID
also returns 503 without redispatch. A new ID cannot bypass the poisoned channel.
A best-effort unknown audit entry names the failed stage; an intent without a
result is also unknown. Firmware execution continues. Concurrent firmware-driven
resets can make attribution uncertain; a generation jump is never acknowledged
as a single successful API reset.

QMP dispatch has a two-second host deadline, each private model exchange a
500 ms socket deadline, and reset observation a three-second host deadline.
The HTTP client should allow at least seven seconds for a complete reset call.
No guest-time wait can extend these bounds. Ordinary scenario timeout/cancellation
still stops the run and its listener.

## Controller power off/on

`POST /api/emu/power` accepts exactly `{"id":1,"on":false}` or
`{"id":2,"on":true}`. Use distinct increasing IDs to power-cycle off then on.
Power IDs have their own namespace, separate from reset and mechanics IDs. The
same authentication, Host/Origin, no-store, strict JSON, 2 KiB/one-second body
limits and single-flight rules apply. `on` must be a JSON boolean.

This is **controller power approximation**, not electrical power removal. External
expanders, motor latches and drum angles remain powered/preserved. It does not
model brownouts, coil heating, UART input loss or network link behavior. A
power-on acknowledges the observed next model generation, not completion of
firmware setup, networking or homing.

- QEMU off executes QMP `stop`, requires a stopped status, and verifies that two
  owner snapshots have identical guest time/state. On performs a hard reset
  while stopped, requires exactly one new generation with unchanged external
  modules/counters, then executes `cont` and requires running status. Flash is
  not recreated. Firmware sees the pinned machine's hard-reset reason; this is
  not claimed to be an electrical power-on reset. Power-on starts execution even
  if QEMU was already paused before power-off. Fidelity is `qmp-stop-reset-cont`.
- Native off checkpoints and fsyncs the locked live model to a
  `controller_power_off` timeline record, replies on the private channel, and
  exits the whole firmware process. The runner waits for successful exit and
  reaps the process group before acknowledging. On launches a **new PID** from
  the existing configuration, NVS and LittleFS, with an explicit power-on resume
  identity and the frozen guest clock/RTC state. Resume restores external state
  and increments the reset generation once; firmware reports `ESP_RST_POWERON`.
  The inherited firmware HTTP listener is reused and the private channel is
  replaced without discarding operation receipts. Fidelity is
  `native-checkpoint-restart`.

While off, `GET /state` returns `live: false`, `power_state: off` and the last
validated physical checkpoint, including its stopped guest time. This cached
snapshot is identified as off; it is not a live reader in a dead native child.
An energized coil remains energized in that external state. Snapshot reads and
mechanics receipt lookup remain available. New mechanics edits and reset
commands return `409 controller_off` without admission or consuming an ID.
Retrying a previously applied reset may still return its original receipt.

A `202` power receipt includes `api_version`, `run_id`, `backend`, `command`,
`status: applied`, `power_state`, `boot_generation`, `guest_ns`, `sequence` and
`fidelity`. A new command targeting the already-current state is an audited
no-op; it does not reset/relaunch. Retrying the latest ID with the same command
returns its original receipt. Reusing it with a different `on`, or submitting an
older ID, returns `409 stale_or_reused_id`. Only the latest power receipt is
retained; `GET /api/emu/power/operations/{id}` reads it independently of the
backend channel. Missing/older receipts return `404 status: unknown`.

Power intents/results are fsynced to `control.jsonl` with `kind: power`. A
checkpoint, transport, launch, observation or audit failure consumes the ID,
marks power state unknown, closes the channel and returns `503 result_unknown`.
It never automatically repeats a transition or launches a second child to guess
what happened. The latest unknown receipt remains queryable, and a new ID cannot
bypass the poisoned channel. Preserve the journal and model timeline for recovery.
These receipts last for this runner's run, including native child replacement,
and do not provide reconnection after runner failure.

Private exchanges have a 500 ms socket deadline; native exit observation is one
second followed by bounded group cleanup, QMP batches share a two-second host
deadline, and power-on generation observation has a three-second host deadline.
Allow at least twelve seconds for an HTTP power request. Scenario deadlines and
cancellation continue on the host clock while off. Teardown first closes control
admission and waits for any admitted transition, then stops the currently owned
child, including one created by power-on. Planned native exit is distinguished
from a crash; every replacement PID and return code is recorded in
`attempts[].power_processes`. Ending a run intentionally off validates its frozen
snapshot rather than inventing a settlement; final metadata retains the off state.
Existing scenario final assertions remain enforced.

## Evidence extension and resume

The immutable `model.json` and manifest hardware remain the initial configuration.
After the first successful edit, model state, activity and timeline records carry
an additive `mechanics_version: 1` extension within existing `schema_version: 1`:
`mechanics_revision`, `initial_mechanics`, `initial_flaps` and
`last_operation`.
Complete snapshots also carry effective `mechanics` and `coils` per module.
Legacy v1 recordings without the extension keep their initial-configuration
interpretation. All new model records also carry the additive `guest_resets`
integer, including when there have been no edits. This preserves reset generation
across native re-exec without requiring an overlay. Legacy records without that
field remain resumable; they cannot reconstruct historical reset counts beyond
the reset event recorded in their last line.

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
Native uses the same C serializer/transport, called by a host thread under
`host::model_lock()`. The thread waits on the private descriptor using host-time
`poll`, so slow virtual clock settings do not delay dispatch. It starts after
model initialization/resume and before firmware setup. The runner owns the HTTP
service and retains its parent socket while the child re-executes. A disconnected
child descriptor stays reserved until process exit, so a later reset cannot
mistake a reused descriptor for the control channel.

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
stalls/anomalies. Pause/resume still uses private QMP in this probe; reset and idempotent retry use
the public API.
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
verification on both layouts. Those earlier runs checked model compatibility;
the native HTTP integration is covered by the additional probe below.

For the mechanics slice, the complete regression suite passes **456 tests**.
The 165 model/control tests also pass with AddressSanitizer and UndefinedBehaviorSanitizer
instrumentation. The native golden source builds and passes shim tests; boot and
Improv reboot verification pass on both layouts, including a fresh reboot check
with the final model. Direct C/HTTP tests cover atomic rejection, open transactions,
physical I2C hall input, revision conflicts, lost acknowledgements, durable command
deduplication, malformed overlays and restoring angles larger than the initial
rotation. The extended QEMU probes exercise edits and deduplication while paused,
then verify persistence through a real controller reset before restoring geometry
and checking strict centered landing. Hosted CI runs these on both layouts along
with the unchanged golden scenarios and runtime image checks.


For native control integration, `tools.verify_native --case control` checks a
live moving snapshot, settled mechanics mutation without angle changes, firmware
Improv software reset, overlay and receipt persistence, and idempotent retry on
both layouts. CI runs this probe with the normal and sanitizer native builds.
The public native runner is also checked for inherited descriptor isolation,
optional dependency validation, listener cleanup after success/failure, and no
control service when disabled. Reset-generation tests cover repeated resets
before any edit, malformed generations and legacy records without the new field.

## Runtime speed

Native `POST /api/emu/speed` accepts exactly `{"id":1,"speed":2.5}`, with a finite
number in 0.25..50 (booleans and strings are invalid). Speed multiplies guest time
relative to host monotonic time; it is a requested clock rate, not a guarantee that
a loaded host can execute firmware at that throughput. It does not change firmware
motor settings, reposition drums or restart the controller.

`GET /api/emu/speed` queries the native owner for `current`, `min`, `max` and
`runtime`; state responses and `control.speed` in the manifest include the last
observed rate. While powered off, the query returns the retained rate. New speed
commands are rejected with `409 controller_off` until powered on. The exact rate
survives native software re-exec and controller power cycling; it does not overwrite
the immutable initial `native.speed` or configuration file.

IDs use a separate increasing namespace, with the same latest-only retry rules as
power. `GET /api/emu/speed/operations/{id}` returns the latest receipt independently
of the child channel. `202 applied` includes the command, effective `speed`, model
`guest_ns`, `sequence`, `boot_generation` and `fidelity: native-virtual-clock`.
Intents and results are fsynced to `control.jsonl` with `kind: speed`. Any uncertain
post-admission outcome consumes the ID, closes the channel and returns
`503 result_unknown`; its unknown receipt remains queryable and the command is
never automatically repeated. A new same-rate command is audited normally.

Clock-owned sleeps wake immediately on rate changes. Timed semaphore/condition
waits, socket polls and DNS waits retain a guest deadline and re-evaluate it on
host slices of at most 5 ms (subject to host scheduling). Readers see monotonic
guest time while a rate is re-anchored. Untimed waits and server housekeeping
remain event/host driven. Run, operation, cancellation and cleanup deadlines remain
host-time bounds, including at 0.25x. Speed zero/pause is unsupported.

QEMU `GET /api/emu/speed` reports `runtime: false` and the startup `timing` preset.
`POST` returns `409 unsupported_capability`: pinned QEMU has no supported live
icount-rate setter. The API never restarts QEMU or substitutes pause/resume for a
speed change. Its `capabilities.speed` remains false.

## Deterministic module faults

`POST /api/emu/modules/0/32/mech` also accepts a strict nested fault patch:

```json
{"id":1,"expected_revision":0,"patch":{"faults":{"sensor":"stuck_inactive"}}}
```

Faults and geometry share revisions, command IDs, audit ordering, latest receipts
and reset/power persistence. Send only fields to change; `ok`
repair a fault without clearing its observed counters. An absent module remains
addressable by the control API so it can be restored.

| Field | Values | Physical meaning |
|---|---|---|
| `sensor` | `ok`, `stuck_active`, `stuck_inactive` | Override the detection returned to firmware before applying configured polarity; snapshots retain the physical Hall level |
| `motor` | `ok`, `dead` | Coils and driver phase update, but valid steps do not move the drum while dead |
| `i2c` | `ok`, `missing` | I²C ACK/NACK presence; disconnecting leaves the external latch, coils and drum angle preserved |

Fault-only patches may apply during motion, between acknowledged I²C transactions.
An active two-byte read/write returns `409 model_busy`; no input is changed halfway
through a latched read. An address NACK has no latched data transaction, so it does
not prevent restoration even on an adapter that supplies no later STOP callback.
Mixed geometry/fault patches retain the existing all-motors-off requirement.
Invalid or busy changes apply nothing and do not consume IDs/revisions. New changes
while the controller is off are rejected as before.

The names and physics come from #19's shared fault model. Startup YAML, for example
`{addr: 32, faults: {motor: dead}}`, also supports noisy/inverted sensors, slip,
disappearance, NACKs and corrupt reads with their required parameters; see
[hardware model](hardware-model.md#injected-faults). Runtime patches accept only
the deterministic literals above. They preserve other configured modes and their
random stream, while repairing a mode clears its now-unused parameter. Faults
never consume geometry/angle draws. Electrical power loss and brownouts remain
unsupported. `fault_support` and `capabilities.set_fault` describe runtime support.

Healthy legacy recordings are unchanged. Once a startup fault or any
runtime fault command is used, `faults_version: 1` and immutable `initial_faults`
identify the extension in state/activity/timeline evidence. Each full module
snapshot includes resolved `faults` (modes, parameters, seed and configured flag)
and `fault_counters` (`blocked_steps`, `nacks`, PRNG `rng`, and total `events`).
The existing top-level #19 `faults` summary is retained and checked against these
records. New native recordings restore the exact random-stream position and effect
counts; older #19 summary-only recordings remain readable and retain their legacy
reseed-on-resume behavior.
The first blocked step/NACK after a change or resume emits `fault_observed`;
subsequent samples/settlements retain cumulative effects without per-step flooding.
`faults_changed` records an admitted edit; a moving edit does not fabricate a
settlement. Readers/replay show the effective historical overlay; native resume
validates it against the initial configuration before restoring dynamic state.

Blocked steps and intentional address NACKs are separate from unexpected stalls
and anomalies. Tests must prove the intended fault effect and unaffected modules,
then restore faults and verify recovery; injecting a fault does not disable normal
error checks or grant a blanket serial-error allowance. The real-firmware probe
accepts only the targeted module's explicitly evidenced address-NACK diagnostic.
