# Split-Flap-Emulator

Run the real [Split-Flap Display](https://github.com/DrewFerg11/Split-Flap-Display) firmware without the hardware.
The emulator runs release builds on an emulated ESP32, with virtual I²C modules, stepper motors, character drums and
hall sensors in place of the real display. Release builds can then be tested automatically in CI or watched on a
virtual display.

![Real v1.4.0 firmware homing 8 virtual modules](docs/images/poc-homing.gif)

## Status

**MVP implementation in progress.** [Phase 1 / PR #9](https://github.com/DrewFerg11/Split-Flap-Emulator/pull/9)
is merged, providing the pinned build and unmodified QEMU library launcher. Phase 2 adds the physical PCF8575, stepper, drum and hall
model, validated 8/16-module hardware, atomic state, activity markers and timelines. Both layouts have booted the
unmodified golden firmware and settled at X with zero stalls/anomalies. Review/merge acceptance is tracked per
phase. The supervised runner arrives in phase 3; `flapsim --help` currently provides the CLI skeleton.

- Findings, results and the step-by-step MVP plan: [`docs/poc-findings-and-mvp-plan.md`](docs/poc-findings-and-mvp-plan.md)
- POC code (reference only): [`poc/`](poc)
- Build, installation, checks and boot reproduction: [`docs/development.md`](docs/development.md)
- Physical hardware, configuration and state/timeline schemas: [`docs/hardware-model.md`](docs/hardware-model.md)
- Ordered branch/PR workflow: [`docs/implementation-queue.md`](docs/implementation-queue.md)
- QEMU integration decision and lifecycle: [`docs/adr/0001-qemu-integration.md`](docs/adr/0001-qemu-integration.md)

## How it works (short version)

- **QEMU**, using the [lcgamboa fork](https://github.com/lcgamboa/qemu) with ESP32 I²C and Wi-Fi support, runs the
  firmware binary unchanged.
- A small hardware model answers the firmware's I²C traffic the way PCF8575 expanders, 28BYJ-48 steppers, drums and
  hall sensors would.
- Tests drive the firmware like a user does (web UI, HTTP, MQTT) and check what the virtual drums show.

## License

GPL-2.0, because the emulator builds on QEMU (GPLv2). Firmware run inside the emulator is not affected by this
licence.

QEMU source is pinned to [`e39508f79cb0681bbe15cfcebd40a3f45cdc3c48`](https://github.com/lcgamboa/qemu/tree/e39508f79cb0681bbe15cfcebd40a3f45cdc3c48).
The build applies no source patches; integration sources are in [`qemu/`](qemu).
