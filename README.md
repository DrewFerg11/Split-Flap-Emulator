# Split-Flap-Emulator

Run the real [Split-Flap Display](https://github.com/DrewFerg11/Split-Flap-Display) firmware without the hardware.
The emulator runs release builds on an emulated ESP32, with virtual I²C modules, stepper motors, character drums and
hall sensors in place of the real display. Release builds can then be tested automatically in CI or watched on a
virtual display.

The Docker image is `ghcr.io/drewferg11/split-flap-emulator`; the command inside it (and the Python package) is
`flapsim`.

![Real v1.4.0 firmware homing 8 virtual modules](docs/images/poc-homing.gif)

## Two ways to run it

The emulator has two modes. Both drive the same virtual hardware (PCF8575 expanders, steppers, drums and hall
sensors), use the same scenario files and produce the same reports.

| | **QEMU mode** (`--mode qemu`, default) | **Native mode** (`--mode native`) |
|---|---|---|
| Runs | The released firmware `.bin`, unchanged, on an emulated ESP32 | The firmware source, built for Linux against host shims of the Arduino/ESP32 APIs |
| Proves | The shipped artifact works | The code behaves correctly at any commit, branch or local checkout |
| Firmware input | `--firmware <factory.bin>` | `--firmware-src golden\|git:<ref>\|<checkout>`, or a prebuilt `--native-build` |
| Speed | About ⅓ of real time | Adjustable virtual clock, 0.25×–50× (default 5×) |
| Network | HTTP through loopback forwarding only | Real host sockets: HTTP, MQTT/Home Assistant and a controllable wall clock |
| Unique coverage | Byte-for-byte release check; wrong and prerelease versions are rejected | MQTT, time and DST scenarios, hours-long soaks in minutes, sanitizer builds |
| Build | Pinned QEMU fork, a few minutes once (cached) | Under a minute |
| Role | **Release gate:** strict and required | **Fast feedback** and coverage QEMU cannot reach; never gates a release |

Use QEMU mode to decide whether a release is good, and native mode while developing firmware or for long and
network-heavy tests. Most scenarios run in both modes; `*-native-*` scenarios are native only. CI runs the golden
v1.4.1 firmware through both on every push.

```sh
# QEMU mode: the released factory image
flapsim run --firmware firmware/splitflap-esp32_n4-v1.4.1-factory.bin --hardware esp32-8-single \
  --scenario scenarios/release-smoke-8.yaml --var FIRMWARE_VERSION=v1.4.1 --out runs/qemu-8

# Native mode: build the golden source, then run the same scenario
flapsim run --mode native --firmware-src golden --hardware esp32-8-single \
  --scenario scenarios/release-smoke-8.yaml --var FIRMWARE_VERSION=v1.4.1 --out runs/native-8
```

Details: [runner](docs/runner.md), [native mode](docs/native.md), [QEMU integration ADR](docs/adr/0001-qemu-integration.md),
[native mode ADR](docs/adr/0002-native-mode.md).

## Documentation

- Build, installation, checks and boot reproduction: [`docs/development.md`](docs/development.md)
- Running the emulator and writing scenarios: [`docs/runner.md`](docs/runner.md), [`docs/scenarios.md`](docs/scenarios.md)
- Physical hardware, configuration and state/timeline schemas: [`docs/hardware-model.md`](docs/hardware-model.md)
- Per-module physical calibration and firmware verification: [`docs/calibration.md`](docs/calibration.md)
- Live authenticated state and mechanics API: [`docs/control-api.md`](docs/control-api.md); read-only live display: [`docs/panel.md`](docs/panel.md)
- Container commands, verified publication and maintenance: [`docs/container.md`](docs/container.md), [`docs/maintenance.md`](docs/maintenance.md)
- Release gate for firmware: [`docs/release-gate.md`](docs/release-gate.md), [`docs/firmware-ci.md`](docs/firmware-ci.md)
- Original findings and MVP plan: [`docs/poc-findings-and-mvp-plan.md`](docs/poc-findings-and-mvp-plan.md); POC code (reference only): [`poc/`](poc)

## How it works (short version)

- **QEMU**, using the [lcgamboa fork](https://github.com/lcgamboa/qemu) with ESP32 I²C and Wi-Fi support, runs the
  firmware binary unchanged. Native mode instead compiles the firmware source against host shims.
- A small hardware model answers the firmware's I²C traffic the way PCF8575 expanders, 28BYJ-48 steppers, drums and
  hall sensors would.
- Tests drive the firmware like a user does (web UI, HTTP, MQTT) and check what the virtual drums show.

## License

GPL-2.0, because the emulator builds on QEMU (GPLv2). Firmware run inside the emulator is not affected by this
licence.

QEMU source is pinned to [`e39508f79cb0681bbe15cfcebd40a3f45cdc3c48`](https://github.com/lcgamboa/qemu/tree/e39508f79cb0681bbe15cfcebd40a3f45cdc3c48).
The build applies no source patches; integration sources are in [`qemu/`](qemu).
