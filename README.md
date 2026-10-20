# Split-Flap-Emulator

Run the real [Split-Flap Display](https://github.com/DrewFerg11/Split-Flap-Display) firmware without the hardware.
The emulator runs release builds on an emulated ESP32, with virtual I²C modules, stepper motors, character drums and
hall sensors in place of the real display. Release builds can then be tested automatically in CI or watched on a
virtual display.

![Real v1.4.0 firmware homing 8 virtual modules](docs/images/poc-homing.gif)

## Status

**Proof of concept complete; MVP phase 1 in progress.** The POC booted the unmodified `v1.4.0` release, homed
virtual drums, and drove them through the firmware's own web API. The first implementation adds a pinned build,
an unmodified QEMU shared-library launcher, and a bounded real-firmware boot/I²C check. Motion simulation and the
supervised runner remain later phases; `flapsim --help` currently provides the CLI skeleton.

- Findings, results and the step-by-step MVP plan: [`docs/poc-findings-and-mvp-plan.md`](docs/poc-findings-and-mvp-plan.md)
- POC code (reference only): [`poc/`](poc)
- Build, installation, checks and boot reproduction: [`docs/development.md`](docs/development.md)
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
