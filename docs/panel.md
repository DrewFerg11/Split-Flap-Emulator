# Live display panel (issue #21, first slice)

`flapsim panel` serves a read-only web page for a run started with `--control-port`.
It shows the virtual display and, for every module, what is physically showing,
its angle, flap error, hall level, coils, energized state, step counters, stalls and
anomalies. It refreshes four times a second, with a short flip when a flap changes.

![Live panel during a native release-smoke-16 run](images/live-panel.png)

```sh
# Terminal 1: a run with the control API enabled (QEMU or native)
flapsim run --mode native --native-build build/native/golden \
  --hardware esp32-16-dual --scenario release-smoke-16 \
  --var FIRMWARE_VERSION=v1.4.1 --control-port 0 --out runs/live

# Terminal 2: the panel for that run
flapsim panel --run runs/live --port 8765
# Panel for runs/live: http://127.0.0.1:8765/
```

## How it works

The [control API](control-api.md) needs a bearer token and rejects browser
origins, so the page cannot call it directly. The panel is a small loopback
server: it serves the page and fetches `GET /api/emu/state` itself, using the
`control.endpoint` and `control.token_file` the run records in `run.json`.

- **Read-only:** the panel only serves `/` and `/state`; it never forwards a
  mutation, and the token never reaches the browser.
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

These remaining #21 items wait on the control API's later slices (#20):

- what the firmware *should* be showing next to what is physically showing
- the I²C monitor and serial console
- fault, mechanics, power and reset controls
- loading firmware, a firmware web-UI link, and a long-running `serve` mode
