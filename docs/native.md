# Native mode

Native mode compiles the Split-Flap Display **firmware source** for Linux against host
implementations ("shims") of the Arduino/ESP32 APIs it uses. It runs that firmware as an
ordinary process driving the same C hardware model as QEMU mode. It complements QEMU mode,
which runs the exact released `.bin`, and does not replace it. Tracking:
[issue #24](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/24).

| | QEMU mode | Native mode |
|---|---|---|
| Input | Released factory `.bin` | Firmware source at a commit |
| Proves | The shipped artifact works | The code's behaviour is correct |
| Speed | ~0.33× real time | 0.25×–50× virtual clock (CI uses 5×) |
| Best use | Release gate (strict, required) | Fast firmware feedback, long runs, network flows |

**Policy:** QEMU mode stays the release gate. Native mode is for fast feedback and for
coverage QEMU mode cannot reach.

## Status

- Phase 1 ([#27](https://github.com/DrewFerg11/Split-Flap-Emulator/pull/27)): the build, the
  shims for everything the firmware touches without a network, and a bounded verifier.
- Phase 2 ([#30](https://github.com/DrewFerg11/Split-Flap-Emulator/pull/30)): host sockets, the
  HTTP transport and `flapsim run --mode native` ([runner](runner.md#native-mode)); the golden
  smoke scenarios run natively in CI under `baselines/v1.4.1-native.yaml`.
- Phase 3 ([#31](https://github.com/DrewFerg11/Split-Flap-Emulator/pull/31)): a supervised
  Mosquitto broker with MQTT scenario steps, a controlled wall clock and seeded settings
  ([scenarios](scenarios.md#native-only-setup-and-mqtt-steps)).
- Phase 4: native mode in the container image ([container](container.md#native-mode-in-the-image))
  and a non-gating firmware CI job for Split-Flap Display ([firmware CI](firmware-ci.md)).

The native runner also supports the shared authenticated
[state and mechanics API](control-api.md) with `--control-port`.

## Build

Requirements: Linux, a C++17 compiler, CMake ≥ 3.20, pkg-config, `libjson-c-dev`, git, and
optionally Ninja.

```sh
python native/build.py                          # golden tag from tools/golden_firmware.json
python native/build.py --firmware-src git:main  # any tag, branch or full commit of the firmware repo
python native/build.py --firmware-src ../Split-Flap-Display  # a local checkout (firmware PRs)
python native/build.py --test                   # also build and run the shim tests
python native/build.py --sanitize --test        # address + undefined-behaviour sanitizers
python native/build.py --export-libraries DIR   # copy the pinned libraries for offline builds
```

`FLAPSIM_NATIVE_CACHE` relocates the source cache. An exported library is used offline when
its `.flapsim-pin` file names the pinned commit. The runner's `--firmware-src` uses
`FLAPSIM_NATIVE_ROOT` (default: this checkout) and builds into `<out>/native-build`, logging to
`<out>/native-build.log`.

The output directory (default `build/native/<commit>-<env>`) holds `bin/flapsim-native`,
`littlefs/` and `build-info.json`. The firmware's compile-time options come from its own
`platformio.ini` environment (`--env`, default `esp32_n4`; only the `esp32dev` board is
modelled). They are applied to the sketch, the firmware and the libraries, as PlatformIO does.

### What is fetched and recorded

- **Firmware**: fetched at build time and never patched or vendored. For `golden`, the fetched
  commit must equal `source_commit` in `tools/golden_firmware.json`. The `.ino` is
  preprocessed the way the Arduino builder does it: `#include <Arduino.h>`, plus prototypes
  for its top-level functions inserted before the first definition, with `#line` directives
  so diagnostics point at the original file.
- **Version**: the firmware's own `inject_version.py` rule (best release tag at `HEAD`, else
  `git describe --tags --always --dirty`); the golden build reports its tag. The build source
  string is `flapsim native`, so the banner reads `Firmware: v1.4.1 (flapsim native)`.
- **Libraries** (`native/libraries.json`), fetched at pinned commits: ArduinoJson 7.4.3 (the
  pin SFD's unit tests use), PubSubClient3 3.3.0 (as in `lib_deps`) and Improv WiFi Library
  0.0.4. ESPAsyncWebServer is replaced by an adapter (see ADR 0002).
- **LittleFS**: `--web-assets data` (default) copies the firmware's JSON data files (for
  example `timezones.json`) using the layout of `gzip_littlefs.py`, with reproducible gzip.
  `--web-assets vite` builds the full web UI with `npm ci && npm run assets` in a scratch copy.
- `build-info.json` and `flapsim-native --build-info` record the firmware
  source/ref/commit/dirty state/version, the environment's defines, the library commits, a
  SHA-256 over `native/` and `model/` (the shim version), the web-asset mode and the compiler.

## Run

The binary takes one JSON configuration:

```json
{
  "schema_version": 1,
  "model_config": "/abs/run/model-config.json",
  "serial_log": "/abs/run/serial.log",
  "serial_input": "/abs/run/serial.in",
  "nvs": "/abs/run/nvs.json",
  "littlefs": "/abs/run/littlefs",
  "speed": 5,
  "random_seed": 1,
  "wifi": {"station": "connect", "connect_delay_ms": 1500, "ip": "127.0.0.1"},
  "http_fd": 3,
  "clock": {"mode": "fixed", "start": 1806195460, "sync": "boot"}
}
```

`http_fd` is an inherited listening socket that serves the device's port 80 (the runner binds
it). `model_config` is the normalized hardware JSON that QEMU mode also uses
(`flapsim.hardware.write_model_config`). Outputs (`state.json`, `activity.json`,
`timeline.jsonl`) have the same schema in both modes. `serial_input` is optional: it is
usually a FIFO, read without blocking, and carries Improv frames or other UART input.

Bounded verification, as CI runs it:

```sh
python -m tools.verify_native --build build/native/<dir> --hardware esp32-16-dual \
  --case improv-reboot --out runs/native/improv-16
```

`boot` requires every module to settle at X under the same fresh-flash serial policy as QEMU
mode. `improv-reboot` additionally provisions Wi-Fi over the serial pipe. It then requires a
software reset that keeps the supervised process, credentials persisted in NVS, a timeline
with exactly one `guest_reset` whose drums, latches and phases continue unchanged, and the
connected boot path homing to `OK` before blanking.

## Fidelity

- **Virtual clock.** `millis()`, `micros()`, `delay()`, `vTaskDelay`, semaphore timeouts and
  the model's `guest_ns` all use one clock running at `speed` × host monotonic time. Rate
  changes re-anchor it, so time never jumps. Boot time restarts at zero on each boot, and guest
  time continues across resets, as QEMU's virtual clock does. The model's 100 ms snapshot
  timer runs on the same clock.
- **Hardware model.** `Wire` is model bus 0 and `Wire1` is bus 1. Transactions use the QEMU
  adapter's event protocol (START_SEND/RECV, data, NACK, FINISH), so the PCF8575, stepper,
  drum and hall model is the same code in both modes. A global lock serializes model calls
  across the two bus tasks. Return codes follow arduino-esp32 2.0.17: 0 ACK, 2 address NACK,
  4 not initialized.
- **NVS / Preferences** reproduce ESP-IDF semantics: typed keys, 15-character names, read-only
  opens of a missing namespace failing with `NOT_FOUND`, and handles invalidated by close. The
  store is persisted atomically in `nvs.json` on every write. `Preferences` keeps the core's
  unsynchronized `started`/`handle` state and its two-step reads, so concurrent tasks race as
  they do on the device. Its error lines match arduino-esp32 2.0.17 in format and source location, for example
  `[  2007][E][Preferences.cpp:50] begin(): nvs_open failed: NOT_FOUND`.
- **LittleFS** is the run's copy of `littlefs/`, with arduino-esp32's `vfs_api.cpp` error
  lines (for example `does not exist, no permits for creation`).
- **FreeRTOS** tasks are detached host threads (the core is recorded only). `loop()` runs on the
  main thread as the Arduino loop task. Counting/binary semaphores and mutexes honour
  virtual-time timeouts. `vTaskDelete(NULL)` ends the calling task.
- **Reset.** `ESP.restart()` brings the model up to date, records `guest_reset` exactly as the
  QEMU adapter's reset handler does, and re-executes the same binary, so the PID and the
  runner's supervision stay unchanged. NVS, LittleFS and `serial.log` persist. The model resumes
  from that record, so external latches and drums keep their state, and the next boot reports
  `Reset reason: software reset`. An uncaught exception or `abort()` prints an ESP32-style
  `abort() was called` record. A fatal signal prints `Guru Meditation Error`. Both reboot with
  `crash/panic`. After a fatal signal, nothing more can be recorded safely: the model resumes
  from its last record, which can lag the drums by up to one 100 ms snapshot of motion.
- **HTTP.** One server thread accepts on the inherited socket, parses HTTP/1.1 requests (16 KiB
  headers, 1 MiB bodies) and runs every handler, concurrently with `loop()`, like the device's
  async_tcp task; each response closes its connection. Routing follows ESPAsyncWebServer 3.6:
  registration order, `uri`/`uri/…` prefixes, JSON handlers needing `application/json` and a
  body below 16 KiB (else 400/413), static files preferring the plain file over its `.gz` twin
  (served with `Content-Encoding: gzip`), and the decoded path without its query string.
- **Sockets.** `WiFiClient` is a host TCP socket (copies share it). Like the device it connects
  only once Wi-Fi has joined a network as a station; the connect timeout is in virtual time.
- **Wi-Fi.** The access point always reports `192.168.4.1`, so scenarios read the same in
  both modes. Station mode associates with any SSID after `connect_delay_ms`, unless `station`
  is `fail`, and then reports `wifi.ip`. Scanning returns one network, `flapsim`. mDNS and OTA
  accept their configuration but do nothing yet.
- **Wall clock.** The firmware's `time()` is interposed (host code keeps the real clock). Like an
  ESP32 before SNTP it counts from power-on (1970) until `configTzTime`/`configTime` has started
  SNTP *and* the station has joined a network, then follows the host's real time
  (`clock.mode: host`) or a fixed start advancing with guest time (`clock.mode: fixed`, which
  scenarios use for deterministic midnight/DST tests; `sync: boot` sets it immediately). The
  synchronized RTC survives emulated software resets. Timezones are applied by the host C library
  from the firmware's POSIX strings.
- **Randomness** (`random()`, `esp_random()`) is a deterministic xorshift seeded from
  `random_seed` and the boot number, not a hardware RNG.

## Limits

- Native mode does not test the shipped binary: not its build flags beyond `-D` defines, the
  partition layout, the PlatformIO/ESP-IDF versions, LittleFS packaging or version injection.
  QEMU mode remains the release gate.
- Concurrency differs: host threads replace two Xtensa cores under FreeRTOS, so races such as
  [SFD#54](https://github.com/DrewFerg11/Split-Flap-Display/issues/54) and the
  [SFD#86](https://github.com/DrewFerg11/Split-Flap-Display/issues/86) UART interleaving may
  reproduce more or less often than on hardware or in QEMU.
- `unsigned long` is 64-bit on the host, so `millis()`/`micros()` never wrap; the device's
  32-bit `micros()` wraps after about 71 minutes.
- The shims must keep up with the firmware's API usage. A missing API fails the build loudly.
