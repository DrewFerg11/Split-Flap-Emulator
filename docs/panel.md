# Live display panel (issue #21)

> **Experimental in 1.0.** The live panel is shipped for early use and may change in 1.x releases while it is finished.

`flapsim panel` serves a web page for a run started with `--control-port`. It is
read-only unless started with `--controls` (see [Controls](#controls)).
It shows the virtual display and, for every module, what is physically showing,
its angle, flap error, hall level, coils, energized state, step counters, stalls and
anomalies. It refreshes four times a second, with a short flip when a flap changes.
Under each tile are the drum-angle dial (needle = drum, dot = magnet), a coil LED and a
hall LED. A tile outlined in red is between flaps: its landing error is over 0.25 of a flap
pitch.

Below the display:

- **Serial console:** the firmware's UART output as it is written to the run's
  `serial.log`, keeping the last 2000 lines. It follows the end of the log unless you
  scroll up.
- **I²C traffic:** the latest 200 transactions (guest time, bus, address, direction,
  data bytes, ACK/NACK and how the transaction ended), newest first, replaying the run's
  retained history when the page opens and following it from there. Start the run with `--i2c-history` as well; without it the section says
  so. A row marked *gap* means records were lost (see
  [the I²C history](control-api.md#structured-i2c-history-and-websocket-stream)).
- **Firmware web UI:** a header link to the firmware's own web server for this run
  (`endpoint` in `run.json`; QEMU forwards it to loopback, native serves it directly).

![Live panel spelling SPLIT FLAP on 16 modules with the real v1.4.1 firmware, going blank, then all modules flipping through to X and back to blank](images/live-panel.gif)

![Live panel during a native release-smoke-16 run](images/live-panel.png)

```sh
# Terminal 1: a run with the control API and I2C history enabled (QEMU or native)
flapsim run --mode native --native-build build/native/golden \
  --hardware esp32-16-dual --scenario release-smoke-16 \
  --var FIRMWARE_VERSION=v1.4.2 --control-port 0 --i2c-history --out runs/live

# Terminal 2: the panel for that run
flapsim panel --run runs/live --port 8765
# Panel for runs/live: http://127.0.0.1:8765/
```

## Controls

`flapsim panel --run runs/live --controls` adds a *Controls* section that sends
commands to the run's [control API](control-api.md):

- **Controller:** reset, power off and power on (the API's controller approximation;
  the drums keep their angle).
- **Fault:** set a module's `sensor`, `motor`, `i2c` or `power` fault to one of the
  runtime modes the API accepts, or back to `ok`.
- **Mechanics:** set one of a module's `sensor_window`, `magnet_position`,
  `flap_offset`, `steps_per_rotation` or `sensor_polarity`.

The result line shows the API's answer, for example `applied`, `HTTP 409 model_busy`
(geometry needs every motor off) or `HTTP 409 controller_off`. Every command is
audited in the run's `control.jsonl` like any other client's.

```sh
flapsim panel --run runs/live --port 8765 --controls
# Panel for runs/live: http://127.0.0.1:8765/#key=…
```

Open the printed URL. Its fragment carries a random key for this panel process.
Browsers never send a fragment to the server, so another local client can read the
page but cannot learn the key from it. The page keeps the key for the tab and removes
it from the address bar. A command is forwarded only when the request:

- comes from the panel's own origin (`Origin` must be present and equal to it, and the
  usual Host check applies),
- carries the key in `X-Flapsim-Panel-Key`,
- is `application/json` of at most 2 KiB, with no duplicate keys and only the fields
  of that command.

The panel chooses each command ID (milliseconds since the epoch, so they keep
increasing across panel restarts and stay above a script's small IDs) and reads
`expected_revision` from live state just before a mechanics command. A script that
sends its own IDs to the same run after the panel has will see `409 stale_or_reused_id`.

Without `--controls` the panel answers every POST with 501 and forwards nothing.

## Home-lab mode: `flapsim serve`

`flapsim serve` keeps one emulator running with this panel and its controls, until
Ctrl-C or SIGTERM (`docker stop`):

```sh
flapsim serve --hardware esp32-16-dual --firmware-release v1.4.2
# Serving esp32-16-dual (qemu) from runs/serve: http://127.0.0.1:8765/#key=…

flapsim serve --mode native --native-build build/native/golden --hardware esp32-8-single
```

It supervises a `flapsim run --scenario hold --control-port 0 --i2c-history` child, so
every run keeps the runner's isolation, evidence and clean shutdown; the runs are
`runs/serve/run-1`, `run-2`, … (with each one's console output in `run-N.log`). A
`hold` run (also usable with `flapsim run --scenario hold`) has no steps and no checks:
it runs until interrupted. Stopping serve stops the current run with SIGINT and waits
for it to finish its evidence.

Besides the [controls](#controls), the panel then shows a **Run** line (the current run,
its firmware and SHA-256, and whether it is still running) with *Restart run*, and in
QEMU mode a **Firmware** line:

- *Load image* uploads a factory image (`application/octet-stream`, at most the 4 MiB
  flash, with an ESP32 bootloader at `0x1000`).
- *Load release* downloads `splitflap-esp32_n4-<tag>-factory.bin` from that
  [Split-Flap Display release](https://github.com/DrewFerg11/Split-Flap-Display/releases)
  (`--firmware-release <tag>` does the same at startup).

Either way the image is kept under `runs/serve/firmware/<sha256>.bin`, the current run
is stopped and a new run starts from a fresh flash with it. Settings saved in the old
run's flash do not carry over. Native mode builds firmware from source, so it offers
*Restart run* only.

v1.4.1 logs its fresh-NVS diagnostic continuously until settings are first saved, so
`serial.log` grows quickly on a new flash (hundreds of MB per guest minute natively).
Save the settings once in the firmware's web UI, or restart the run, to keep it small.

### Beyond loopback

By default serve listens on `127.0.0.1:8765` only. To use it from another machine,
for example a Proxmox LXC or VM:

```sh
flapsim serve --hardware esp32-16-dual --firmware-release v1.4.2 \
  --bind 0.0.0.0 --allow-host splitflap.lan:8765
```

`--allow-host` adds the `Host` (and `Origin`) the browser will send; requests for any
other name are still refused, which keeps the DNS-rebinding defence. Open the printed
`http://splitflap.lan:8765/#key=…` URL. Anyone who can reach the port can watch the
display, serial and I²C traffic; commands still need the key, which travels without
TLS, so use this on a trusted network or behind a TLS reverse proxy. The *Firmware web
UI* link points at the firmware's loopback server and only works on the machine
running serve.

## One look across the UIs

The live panel and the run's `replay.html` share one dark style and one display script
(`flapsim/data/ui/theme.css` and `display.js`, derived from the POC viewer). They are
inlined into each page when it is served or written, so both stay self-contained, with no
external assets and the panel's strict Content-Security-Policy unchanged. The replay adds
play/pause, a scrubber and a collapsible *Snapshot details* panel with the per-module table.
Its dials use the run's configured mechanics until a snapshot records a change.

## How it works

The [control API](control-api.md) needs a bearer token and rejects browser
origins, so the page cannot call it directly. The panel is a small loopback
server: it serves the page and fetches `GET /api/emu/state` and
`GET /api/emu/i2c` itself, using the `control.endpoint` and `control.token_file`
the run records in `run.json`. It reads `serial.log` from the run directory.

| Route | Answers |
|---|---|
| `/` | the page |
| `/state` | the control API's live state |
| `/i2c?since=<cursor>` | one page of I²C history after an opaque cursor (the page's `next`) |
| `/serial?since=<offset>` | serial output after a byte offset, at most 64 KiB; without `since`, the last 64 KiB. `restarted` means the log was replaced or is briefly missing (a QEMU startup retry) and the console clears and reads again from 0 |
| `/run` | `firmware_url`: the run's loopback firmware web server, or null |

- **Read-only by default:** without `--controls` the panel only serves those GET
  routes and never forwards a mutation. With it, only `POST /control/reset`,
  `/control/power` and `/control/mech` are added. The token never reaches the browser.
  Unknown or repeated query parameters answer 400.
- **Serial floods:** a console more than 256 KiB behind skips to the newest 64 KiB and
  shows how many bytes it skipped; `serial.log` keeps them.
- **Firmware output is text:** serial lines and I²C bytes are inserted as text,
  never parsed as HTML.
- **Loopback only:** it binds `127.0.0.1` and rejects foreign `Host`/`Origin`
  headers, as the control API does. Its responses are `no-store`, with a strict
  Content-Security-Policy.
- **Follows the run:** it re-reads `run.json` on every request, so it can start
  before the run's control API is up. Until then, and after the run ends, `/state`
  answers 503 with the reason, and the page shows it.
- **No extra dependencies:** it uses the standard library HTTP server.

The panel and the run must share a loopback interface. In Docker, run both in the
same container, or use `--network host` on Linux.

## Not yet

Remaining #21 item:

- what the firmware *should* be showing next to what is physically showing. v1.4.1
  exposes no target position (no route, I²C or serial signal reports it), and the
  emulator does not reimplement firmware logic, so this needs a firmware-side signal
