# Issue #20: live emulator control API — research and implementation plan

Status: proposed; research only, no control API implemented by this document.
Research date: 2026-10-21. Tracking: [#20](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/20).

## Sources and exact revisions

- Main: `885b7657012a39acb35017c81f23f3348c050704`, the merge of
  [#29](https://github.com/DrewFerg11/Split-Flap-Emulator/pull/29), which implements
  per-module calibration (#18). Research branch starts here.
- Native mode is an open stack, not a merged feature:

  | PR | Head inspected | Scope / state |
  |---|---|---|
  | [#27](https://github.com/DrewFerg11/Split-Flap-Emulator/pull/27) | `0ed2ca3dfaac9d97d429c0dfcdf4a8a5ca98dbab` | Host shims, shared model, resume; ready |
  | [#30](https://github.com/DrewFerg11/Split-Flap-Emulator/pull/30) | `0fcff6d095870456ddaa7916caec9c7799b63878` | Native runner, sockets, firmware HTTP; draft on #27 |
  | [#31](https://github.com/DrewFerg11/Split-Flap-Emulator/pull/31) | `139c5734a0219bfee830924ff642ed0c5aa727f0` | MQTT, NVS seeds, controlled wall clock; draft on #30 |
  | [#32](https://github.com/DrewFerg11/Split-Flap-Emulator/pull/32) | `9bc7297765c2cd3387e0f1eab608ba7dda211378` | Container and firmware CI; draft on #31 |

  Read the cumulative #32 tree in a separate detached worktree. Its merge base
  with current main is `e197cd7`: it does **not** yet include #18's per-module
  sensor width/polarity. Recheck heads, rebase integration and CI before coding
  against this stack. Its earlier realistic-preset results describe older mechanics.
- Pinned QEMU source: `e39508f79cb0681bbe15cfcebd40a3f45cdc3c48`.
  Read `qapi/machine.json`, `qapi/misc.json`, `qapi/run-state.json`,
  `accel/tcg/icount-common.c`, `util/main-loop.c`, and ESP32 reset handling.
- Prior art: avandeputte/SplitFlapEmulator at
  `695eda6c1ddfc638a86b85790be5d0864f5c7763`, especially
  `emulator/sfemu/app.py`, `supervisor.py`, `common/vclock.h`, and README API.
  It uses a supervisor/hub with separate firmware processes. Its protocol,
  serial-number module addressing and power topology differ from ours. Its
  CC-BY-NC-SA-4.0 code is not compatible with copying into this GPL-2.0-only
  project; use it as behavioral reference and write the implementation afresh.
- Read related [#19](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/19)
  (faults), [#21](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/21)
  (live panel/serve), and [#22](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/22)
  (first boot). Keep those responsibilities distinct from the transport work.

## What the code establishes

1. `model/` is process-local C state. QEMU calls it through `model/adapter.c`;
   native `Wire.cpp`, its snapshot thread and reset path hold
   `host::model_lock()`. HTTP threads must never mutate this state directly.
2. `state.json` is a **settlement**, not the latest moving state. Moving frames
   go to `timeline.jsonl`, while `activity.json` contains identity/counters but
   no module positions. Serving `state.json` as current state would be wrong.
3. `fs_model_guest_reset()` clears in-progress bus transactions but preserves
   drum angles, external PCF8575 latches and phases. Preserve this contract.
   QEMU already supplies a private QMP socket, currently used only at startup.
4. Native `ESP.restart()` records the model reset, closes outputs and re-execs
   the same binary/PID. `fs_model_resume()` reloads initial geometry from config
   and restores dynamic state from the last complete timeline record. It does
   not restore runtime geometry/fault overlays, which do not yet exist.
5. Native #30 owns a separate inherited listener for the **firmware's** port 80.
   It survives re-exec. The emulator control service belongs to the runner and
   must have its own listener; do not add handlers to the firmware shim server.
6. QMP `system_reset` is documented as a hard reset. `system_powerdown` is only
   a request which the guest may ignore, not a power switch. No live icount
   mode/shift setter was found in the pinned QAPI; startup `--timing` remains
   authoritative. Pause/resume is a separate operation, not a speed change.
7. Native has `vclock::set_speed(0.25..50)`, but existing sleeps are not notified
   on acceleration, and `vclock::wait_for` fixes its host deadline at entry.
   A standalone GCC C++17 probe against the inspected `vclock.cpp` started a
   100 ms virtual wait at 0.25x, switched to 50x after 20 ms wall, and observed
   **400.4 ms wall** for both `sleep_ns` and condition-variable `wait_for`.
   This is a measured limitation, not proof about all firmware timing paths.
   Dynamic-rate support needs wait notification/recomputation and concurrent
   monotonic-clock tests, including readers racing a re-anchor.
8. Existing `--trace` is optional text, emitted before the event return value,
   without a replay cursor. It cannot supply a reliable transaction/ACK stream.
9. Python settlement validation derives geometry from the initial hardware
   configuration. Runtime geometry changes require evidence and validator work,
   not merely a setter in C. Native source baselines and QEMU binary baselines
   also remain separate identities after the native stack lands.

## Recommended architecture

One runner-owned control server per board/run, shared across QEMU and native.
Keep `manifest.endpoint` as the firmware HTTP endpoint; add a separate
`control` object with API version, endpoint, enabled state and capabilities.
Enable with `--control-port PORT` (`0` requests an OS-assigned port), binding
loopback by default. No server/dependency overhead for an ordinary run without
control. The existing scenario completion/deadline still ends the run; persistent
`serve` and the web UI belong to #21.

Use one backend interface (`snapshot`, `capabilities`, `submit`, lifecycle and
telemetry), not two independent HTTP implementations. Add an optional control
client to `RunContext` **after** reconciling #30/#31's services/context changes.
Keep process supervision, signals, finalization and MQTT ownership in the runner.

Recommended HTTP stack: Starlette + Uvicorn with the wsproto WebSocket backend,
as an optional `control` extra, with explicit pinned-compatible dependency ranges
and license/version checks at implementation time. This avoids hand-writing a
WebSocket/HTTP parser. The runner retains signal handling; the server's loop has
a bounded shutdown path. Confirm this packaging/threading arrangement in a small
spike before freezing the dependency choice.

Use private inherited Unix socketpairs for child control, with bounded framed
JSON messages, protocol version, run/boot generation and command IDs. Keep
telemetry separate from command replies so heavy I2C traffic cannot starve a
reset or acknowledgement. Preserve/re-establish native descriptors across exec;
new boot handshakes reconcile commands whose reply was interrupted by reset.
Do not use config-file rewrites as an IPC protocol.

- QEMU adapter: marshal commands onto the model's owner context under the
  correct QEMU lock. Investigate the pinned `qemu_set_fd_handler`/bottom-half
  APIs, verify exported symbols and locking, and prove servicing while paused.
  A virtual-clock-only polling timer cannot service commands when guest time
  stops. Do not patch QEMU or invoke the model from an arbitrary socket thread.
- Native adapter: use the existing model lock around snapshots/changes, with
  bounded contention and a documented lock order. Do not hold it across network
  writes, clock waits or a runner acknowledgement. Never call a reset helper
  while already holding the nonrecursive model lock it acquires itself.
- Apply mutations between complete I2C transactions. QEMU delivers transaction
  events individually; defer affected-module changes until its bus is closed,
  or return a bounded busy result. Never change an input halfway through the
  two bytes of a latched read. Native already locks complete Wire transactions.

## Proposed v1 API contract

Prefix `/api/emu`; responses include `api_version`, run ID and boot generation.
Use numeric bus IDs 0/1 and decimal numeric addresses in URLs (32..39 today);
return both numeric identity and a display-friendly hexadecimal address.

| Route | Contract |
|---|---|
| `GET /state` | Coherent current model snapshot, active mechanics/faults, angle, char, fractional position/error, latch/coils/phase, electrical hall, counters, guest time, power/lifecycle state, backend and capabilities |
| `POST /modules/{bus}/{addr}/mech` | Strict atomic partial update of supported physical fields/faults; no firmware-settings writes |
| `POST /reset` | Controller reset, preserving external mechanical state and persistent settings; backend-specific reset fidelity is reported |
| `POST /power` | `{on: false}` / `{on: true}` for controller power; external modules remain powered; see lifecycle requirements below |
| `POST /speed` | Native `{speed: number}` in 0.25..50; QEMU reports startup timing and rejects unsupported live changes |
| `GET /i2c?since=CURSOR&limit=N` | Ordered structured records, next cursor, retained range and explicit gap information |
| `WS /stream?since=CURSOR` | The same I2C records plus typed state/control/lifecycle events; reconnect and slow-client rules below |
| `GET /operations/{id}` | Accepted/applied/failed/unknown status and observed completion for asynchronous controls |

Document capabilities per backend and current lifecycle state. Unsupported
features return `409` with a stable `unsupported_capability` code and reason;
never claim success or silently restart to switch QEMU timing. Unknown modules
return 404, invalid payloads 422, malformed JSON 400, size limits 413, full
queues 429 and unavailable/rebooting backends 503. Method mismatches return 405.
Bound bodies, queues, connections and operation retention.

Mutations return 202 with an operation ID after strict validation/queue admission;
this means **accepted**, not applied. Completion includes model sequence and
guest time. Reset/power-on complete only after the next backend/model handshake,
with a separate boot-ready observation if requested. Accept client command IDs
for deduplication within a documented retention window. A lost acknowledgement
must become queryable or explicitly unknown; never automatically repeat a reset.
Write control intents/results to `control.jsonl` and correlate applied model
events by ID. A crash between intent, application and acknowledgement must not
be reported as a completed operation.

Loopback binding alone does not protect browser-driven mutations: reject foreign
Origins, validate Host, require JSON for mutations, disable permissive CORS, and
validate WebSocket Origins too. Require a per-run bearer token for mutations
and streaming; keep it out of URLs, logs and manifests. An explicit external
bind must require authentication; document container port exposure separately.

## Mechanics, faults and evidence

Reuse #18's field names and range validation: magnet position, sensor window,
polarity and flap zero. Runtime values must be resolved literals, not a new
random draw. Keep topology and flap character order fixed for v1. A rotation
size change needs explicit semantics: allow only when settled and the existing
angle and all resulting geometry fit the new rotation; otherwise reject the
whole patch. Never silently move the drum, wrap an invalid angle or recalibrate
firmware to manufacture a passing landing.

Add a monotonic mechanics revision and immutable initial config. Each complete
snapshot/control event identifies its effective resolved mechanics and faults;
historical validation/replay uses that record's revision, not today's settings.
Native resume restores the effective overlay before checking dynamic angles and
derived flap data. Preserve compatibility for existing v1 recordings with no
runtime overlay. Define and test the smallest versioned schema extension before
changing writers, readers, resume, report and replay together.

#19 owns fault physics. Integrate through a shared strict fault registry rather
than inventing a second fault implementation in HTTP. Coordinate a minimal
deterministic subset (hall stuck active/inactive, dead motor, module absent)
before marking #20's fault mutation/scenario acceptance complete. Specify hall
faults in terms of physical detection versus raw electrical level so active-low
sensors are unambiguous. Unsupported noise/slip/NACK/corruption faults must be
advertised as unavailable until #19 implements them. Their future PRNG streams,
draw count and reset persistence must be recorded for reproducibility.

Injected faults need their own expected/observed evidence; do not disable
`assert_no_errors` or broadly baseline all subsequent stalls/anomalies. Scenarios
must assert the intended firmware response and that unaffected modules work.

## Reset, power and speed: required spikes

- **Reset:** exercise QMP hard reset on the pinned ESP32 machine, verify actual
  reset reason, exactly one model reset, continuous angles/latches/counters,
  preserved flash/settings and a live control endpoint. Native resets should
  reuse the existing re-exec path and preserve overlays and effective speed.
  Do not label QMP hard reset as a firmware software reset without evidence.
- **Controller power:** explicitly excludes removing power from PCF8575/motors
  or simulating brownouts (#19). Off means the controller executes no firmware;
  external drum angle/latches remain. Control HTTP remains available, and an
  off-state response labels its last physical snapshot and stopped clock.
  Refuse mechanical mutations while off until safe off-state support exists.
- **QEMU power candidate:** QMP stop while off, hard reset then cont on power-on.
  Prove reset reason, stopped CPU/time, persistent flash and absence of movement
  before reset. This is a controller power approximation, not electrical fidelity.
  `system_powerdown` and merely relabeling a reset are not substitutes.
- **Native power candidate:** checkpoint the locked model, intentionally stop
  and reap the child, retain NVS/LittleFS/overlay/clock state, then start a new
  child in a power-on resume context. This requires an explicit runner lifecycle
  abstraction: planned power-off must not trigger `RunContext.checkpoint()`'s
  ordinary early-exit failure. Software re-exec retains its existing same-PID
  contract; an intentional power cycle may have a new PID. Never delete outputs
  or reset initial angles through the current fresh-run `prepare()` path.
- Both candidates require experiments before committing to full power support.
  Until verified, advertise reset separately and power as unsupported; keep the
  corresponding issue checkbox open. No silent semantic downgrade.
- **Speed:** wake/re-evaluate outstanding native virtual deadlines on rate
  changes, test acceleration/deceleration and monotonic readers, and retain the
  current rate across re-exec. Include delays, FreeRTOS semaphore/task waits,
  socket timeouts and model sampling. Runner operation/run deadlines remain host
  monotonic time; a paused or very slow guest must not defeat cleanup timeouts.

## I2C stream and snapshot consistency

Instrument the shared C event boundary after the result is known. Capture raw
event kind/data, return value/ACK, bus/address, guest time, transaction ID and
monotonic sequence. Transaction grouping must represent malformed/incomplete
transactions and reset termination honestly, not fabricate a FINISH. Both
backends feed the same representation; the existing text trace remains usable.

Use opaque, run-scoped cursors (decimal 64-bit sequence strings avoid JavaScript
integer truncation), bounded retained history and bounded subscriber queues.
Pagination returns the **earliest** records after a cursor, not the last N while
skipping unseen records. Return explicit expired-cursor/gap metadata, including
drops before collection; never silently lose events. A slow WebSocket client
must not block firmware/model progress: report a gap or disconnect with a
documented resync procedure. Rate-limit/coalesce state notifications separately
from transaction records. Persist enabled structured telemetry as `i2c.jsonl`
with its retention/loss policy recorded; do not append every byte to the normal
physical timeline. Handle partial final records, resets and shutdown flushing.

A snapshot comes from the owner-locked model serializer, not independent file
reads or reconstructed byte traffic. It includes whether it is live, off or
temporarily unavailable. Separate snapshot serialization from timeline emission
so HTTP reads do not inflate movement/settlement IDs or alter model behavior.

## Delivery sequence and verification gates

1. **Contract and integration spike.** Recheck native/main heads; reconcile #18
   with #27/#30, preserving both sensor geometry and resume. Freeze API/evidence
   schemas, backend capabilities and lock/lifecycle rules. Prototype owner-context
   QEMU IPC (including paused mode), native exec reconnection and HTTP shutdown.
2. **Shared read/control foundation.** Add model snapshot/overlay interfaces,
   private IPC, runner server and state/capability endpoint for both backends.
   Implement typed operations and audited mechanics changes. Extend native
   resume and all evidence readers in the same change. Verify controls disabled
   preserve existing run behavior/performance and artifacts.
3. **Lifecycle, speed and deterministic faults.** Resolve the power spikes;
   implement truthful reset/power and native speed, with explicit QEMU limits.
   Integrate #19's initial fault subset. Test repeated resets and control requests
   during motion, busy transactions, crashes and shutdown, not just idle boots.
4. **Streaming and scenarios.** Add structured I2C history/WebSocket delivery,
   shared `set_mechanics`, `set_fault`, `reset`, `power_cycle`, `set_speed` steps.
   Steps use the same typed dispatcher as HTTP and await applied/observed
   completion. Retain `http` steps for the firmware endpoint; never redirect them
   to the control port. Reject unsupported mode/capability combinations before
   child startup. Automatically provision internal control for control steps,
   without requiring an exposed HTTP listener for a headless scenario.
5. **Packaging and final acceptance.** Update installed resources, CLI docs,
   optional dependency installation and the container/native layout from #32.
   Run existing golden/native matrices, control scenarios, process cleanup and
   exact-image checks. Preserve QEMU as the strict shipped-binary release gate.
   Split into reviewable PRs; do not mark #20 done after only the read API lands.

Required tests/evidence:

- C: atomic valid/invalid patches, polarity/wrap boundaries, no mutation on
  rejection, command/transaction ordering, snapshot consistency, overlay resume
  and reset preservation. Faults must change physical/electrical behavior through
  real I2C responses; firmware settings never change hidden model geometry.
- API: malformed/oversized bodies, duplicate/unknown fields, booleans in numeric
  fields, non-finite values, nonexistent modules, forbidden Origin/Host/token,
  method errors, concurrency, queue limits, replayed IDs and lost replies.
- Streams: cursor ordering/pagination, expired cursors, gaps, slow/disconnected
  subscribers, reset/re-exec continuity, transaction result bytes and no leaks.
- Native: real firmware against both layouts; speed during existing sleeps,
  reset after mechanics/fault changes, queued HTTP through reboot, NVS continuity,
  MQTT/clock scenarios unaffected. Use clean source provenance for its baseline.
- QEMU: real pinned binary on both layouts; reset while moving preserves physical
  state, mechanics/sensor changes affect I2C, correct unsupported speed response,
  and power behavior only after the candidate passes the lifecycle spike.
- Lifecycle: timeout/SIGINT/SIGTERM, child crash, off-state shutdown, active WS,
  repeated reset, occupied ports and multiple simultaneous runs leave no child,
  listener, socket or broker behind; finalize artifacts once, including failures.
- Evidence: original firmware/executable unchanged; record exact source/build
  revisions, backend, capabilities, commands and effects. No new generic timeout,
  alignment or settings-race baseline allowances. Keep calibration #18's strict
  quarter-pitch assertion and its documented SFD#54 limitation intact.

## Remaining decisions before implementation

The architecture above is a recommendation, not verified runtime support.
The focused spikes must settle QEMU IPC locking/exported symbols, actual reset
reason and controller-power approximation, native power checkpoint/resume and
virtual-wait wakeups, and the optional HTTP stack's packaging/shutdown behavior.
These are engineering checks to perform; they do not require the user to choose
routine implementation details. The native stack need not all merge before a
contract is written, but the runner integration should build on the reconciled
#27/#30 interfaces rather than introduce a competing native runner. #31/#32 are
compatibility targets, and final verification must use their eventual merged heads.


## Implementation progress

The first slice (#34) shipped the QEMU read API and shared C snapshot/transport,
based on merged native #27. The second slice (#36) added atomic, settled/idle-only mechanics
edits, durable model revisions/receipts, runner audit and overlay-aware native
resume/readers. It uses bounded synchronous application before a 202 `applied`
response rather than introducing an asynchronous admission queue. Operation lookup
retains the latest applied receipt, and a durable ID high-water mark prevents
older edits being repeated. See [the implemented contract](control-api.md) for
limits and recovery semantics. The third slice integrates the shared HTTP service
with the merged #30 native runner, using the model mutex and retaining the private
channel across firmware re-exec. Reset generations now persist in every model
record, including before any mechanics edit. Lifecycle, faults, speed, streaming and scenario control steps
remain later work; #20 is not complete.
