# ADR 0001: Unmodified QEMU library with a process-local C launcher

Status: accepted after the real-firmware callback spike; phase-1 CI/merge acceptance remains in progress.
Tracking: [issue #2](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/2).

## Context and spike

The frozen POC patches `esp32_picsimlab.c` to include its hardware model. The
plan prefers route B: use the fork's callback registration API without modifying
QEMU source, provided a bounded spike proves the actual firmware reaches it.

Inspection of lcgamboa QEMU at
`e39508f79cb0681bbe15cfcebd40a3f45cdc3c48` established:

- `hw/xtensa/esp32_picsimlab.c` defines `callbacks_t` and
  `qemu_picsimlab_register_callbacks(void *)`. Every callback pointer must be
  valid; registration replaces the harmless upstream placeholders. A null
  `pinmap` is supported by the machine initialization code.
- `hw/i2c/picsimlab_i2c.c` forwards bus start/finish, byte write/read and NACK
  events, including controller ID and address. The event ABI is in
  `include/hw/i2c/i2c.h`: START_RECV=0, START_SEND=1, START_SEND_ASYNC=2,
  FINISH=3, NACK=4; this fork encodes write data as `(data << 8) | 5`, read as 6.
- `system/main.c` calls `qemu_init(argc, argv)`, `qemu_main_loop()`, then
  `qemu_cleanup(status)`. This is the lifecycle the launcher reproduces.
- `build_libqemu-esp32.sh` builds with `-fPIC` and derives a shared-library link
  command from Ninja's executable link flags, omitting `system_main.c.o`.
  Its full script also builds RISC-V and debug targets, which this MVP does not
  need. Our build keeps the same link recipe but selects only Xtensa and fails
  explicitly if the expected main object/output changes.

## Decision and lifecycle contract

Use one production integration: route B. `qemu/build.sh` builds the unchanged
upstream Xtensa target, derives `libqemu-xtensa.so` from its exact link command,
and compiles `qemu/launcher.c`. The callback ABI is recorded in
`qemu/callbacks.h` and must be checked on every QEMU pin bump. No fallback patch
is part of the production build.

The launcher resolves its own `/proc/self/exe` path, loads the sibling
`../lib/libqemu-xtensa.so` with `RTLD_NOW | RTLD_GLOBAL`, and validates all four
required lifecycle/registration symbols. It registers a complete callbacks
table before `qemu_init`. Initialization, the main loop, and cleanup all run
sequentially on the process's original thread. Callback storage lives until
process exit; the library stays loaded because QEMU has global and atexit
state. Missing libraries/symbols fail with a nonzero exit and stderr diagnostic.

One board uses one launcher process. Do not initialize QEMU a second time or
run multiple boards in one address space. QEMU installs its own signal handlers;
the supervising Python process owns the child's process group and sends
SIGTERM, then SIGKILL after a bounded grace period if needed. Guest resets do
not restart the launcher or replace callback storage. Phase 2 will preserve
physical drum state across those resets.

## Executable contract for phase 3

The staged prefix contains:

```text
bin/flapsim-qemu
lib/libqemu-xtensa.so
share/qemu/esp32-v3-rom.bin
share/qemu/esp32-v3-rom-app.bin
build-info.json
```

The launcher accepts the same arguments as `qemu-system-xtensa`; the runner must
pass `-M esp32-picsimlab`, `-L <prefix>/share/qemu`, a private 4-MiB flash copy,
serial output, networking and timing arguments. `flapsim-qemu --version` is a
lightweight loader check. stdout/stderr are emulator diagnostics; guest UART0
is the explicitly configured serial file. Return status comes from QEMU, but
an exit status alone does not establish firmware/test success.

The temporary `qemu/boot_stub.c` only ACKs bus 0 address 0x20 and returns 0xff
on reads. It cannot simulate motion or hall timing. Phase 2 replaces this
translation unit with the production hardware model; it does not add a second
integration. Phase 3 replaces the temporary harness's process supervision, and
phase 4 replaces its assertions with full scenarios and reports.

## Verification and consequences

Reproduction commands, input checksum and log locations are in
[`../development.md`](../development.md). The harness verifies the real banner,
the firmware's own bus-0 discovery of 0x20 (not its configuration listing), and
the callback ACK log against the unchanged published factory image. It also
rechecks the input checksum after the emulator has written to its private flash.

The bounded spike succeeded on 2026-10-19 (America/Indianapolis). The unmodified
published image printed `=== Split Flap Display ===`, `Firmware: v1.4.0-rc.1`,
and the scanner result `Wire  : [0]0x20`. stderr recorded
`FLAPSIM probe: bus=0 address=0x20 ACK event=1`. The verifier passed in 41.591
wall seconds, rechecked the original SHA-256, and terminated QEMU through its
normal signal path with return code 0. The loaded engine reports QEMU 9.2.2.
Evidence is retained in `runs/boot-foundation/verification.json`, `serial.log`,
and `qemu.log`; CI retains the equivalent outputs as `qemu-boot-evidence`.

Local host: Debian 13, x86-64, GCC 14.2, Python 3.12.14, three visible CPUs with
a two-CPU cgroup quota. A rootless dependency repair required an incremental
rebuild, measured at 699.222 seconds with three jobs. CI records cold-build
timing and its full runner/toolchain details for comparison with the plan's
roughly ten-minute/four-vCPU target. The phase remains in progress until its PR
is merged and acceptance passes. Twenty fast checks, Ruff, clang-format 18,
and actionlint passed locally.

Route A remains a documented contingency if a future pin loses this library
ABI or lifecycle. It would require a new decision and a minimal patch with
model source staging. Maintaining both routes now would duplicate build and
regression work without a proven need. The existing patch in `poc/` remains
historical reference only.
