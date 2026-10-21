# ADR 0002: Native mode built from firmware source against host shims

Status: proposed with phase 1 of [issue #24](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/24).

## Context

QEMU mode runs the released binary unchanged, which makes it the right release gate. It runs
at about a third of real time, though, and it cannot join a WPA2 network, so it has no MQTT,
NTP or network OTA path. Split-Flap Display (SFD) already compiles its sketch on the host for
unit tests, which shows the source builds outside PlatformIO. Native mode builds that same
source against working (not recording) Arduino/ESP32 shims and drives the existing C model.

## Decisions

1. **Shims live here, written fresh under GPL-2.0.** SFD's firmware code and its
   `tests/support` fakes have no explicit licence yet, so they are not copied. The arduino-esp32
   core (LGPL/Apache-2.0) and ESPAsyncWebServer (LGPL-3.0) are not copied either. The shims
   reproduce their documented behaviour and their error log lines, which the verifiers and
   baselines compare.
2. **Firmware source is fetched at build time and never patched** (issue option (a)). The
   golden build is bound to `tools/golden_firmware.json`'s commit, and a local checkout or any
   firmware ref can be built for firmware PRs. Offline vendoring (option (b)) is not needed.
   The `build/native/cache` directory makes rebuilds offline once fetched.
3. **ESPAsyncWebServer is an adapter, not the real library.** The real library depends on
   AsyncTCP and lwIP internals. The adapter keeps the handler API and the 3.6 routing rules
   (registration order, URI prefix rules, JSON content-type and 16 KiB limits, static `.gz`
   twins). Every handler runs on one server thread, concurrently with `loop()`, matching the
   device's async_tcp task, so the settings race stays reproducible.
4. **One virtual clock.** Firmware time, FreeRTOS waits and the model timeline share it.
   Guest time continues across resets, while boot time restarts, as with QEMU's virtual clock
   and the device's `esp_timer`.
5. **Reset by re-executing the process.** A device reset re-runs every static constructor. An
   in-process restart (for example `dlclose`/`dlopen` of the firmware) would have to tear down
   arbitrary firmware threads and globals reliably. `execve` of the same binary gives a truly
   fresh firmware image and keeps the PID the runner supervises. The C model gained one entry
   point, `fs_model_resume`. It reloads the config and restores the `guest_reset` record that
   the QEMU-equivalent reset path writes first, so the model code stays shared.
6. **Identity is recorded, not assumed.** The firmware commit/ref/dirty state/version, the
   PlatformIO environment's defines, the library commits and a digest of `native/` and `model/`
   are compiled into the binary and written to `build-info.json`.

## Consequences

- Native results describe the source and the shims, not the shipped artifact; QEMU mode stays
  required for releases.
- Shim fidelity is part of the test surface. The behaviour that the firmware and the
  baselines depend on is pinned by `native/tests/shim_tests.cpp` and by the native verifier.
- Thread-level races differ from two-core FreeRTOS (see [native mode limits](../native.md#limits)).
