"""Read-only live display panel for a running emulator (#21).

The control API (#20) rejects browser origins and needs a bearer token, so a page can't
call it directly. This small loopback server serves the panel page and fetches state and
I2C history for it server-side, using the run's own token file, and tails the run's serial
log. It never forwards mutations.
"""

import http.client
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from flapsim.control import accepts_authority
from flapsim.ui import assemble

STATE_TIMEOUT = 2.0
MAX_STATE = 1024 * 1024
SERIAL_CHUNK = 64 * 1024
# Telemetry cursors are opaque, but only ever a run ID and a decimal sequence.
CURSOR = re.compile(r"[A-Za-z0-9_.:-]{1,200}")


class PanelError(RuntimeError):
    """The run has no live control API to read (yet, or any more)."""


def manifest_of(run_dir):
    try:
        manifest = json.loads((run_dir / "run.json").read_text())
    except (OSError, ValueError) as error:
        raise PanelError(f"no readable run.json in {run_dir}") from error
    if not isinstance(manifest, dict):
        raise PanelError(f"no readable run.json in {run_dir}")
    return manifest


def loopback_port(url):
    """The port of an http://127.0.0.1:<port> URL, or None for anything else."""
    try:
        parts = urlsplit(url)
        port = parts.port  # raises ValueError for a non-numeric or out-of-range port
    except (TypeError, ValueError, AttributeError):
        return None
    if parts.scheme != "http" or parts.hostname != "127.0.0.1" or not port:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        return None
    return port


def control_target(run_dir):
    """The control endpoint and token recorded by a run started with --control-port."""
    manifest = manifest_of(run_dir)
    control = manifest.get("control")
    if not isinstance(control, dict) or not control.get("endpoint"):
        raise PanelError("the run has no control API; start it with --control-port 0")
    if control.get("status") == "stopped":
        raise PanelError("the run has finished; its control API is stopped")
    port = loopback_port(control["endpoint"])
    if port is None:
        raise PanelError("the control endpoint is not a loopback HTTP address")
    try:
        token = Path(control["token_file"]).read_text().strip()
    except (KeyError, TypeError, OSError) as error:
        raise PanelError("the run's control token file is not readable") from error
    return port, token


def fetch(run_dir, path):
    """GET a control API path with the run's token: (HTTP status, decoded JSON body)."""
    port, token = control_target(run_dir)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=STATE_TIMEOUT)
    try:
        connection.request(
            "GET",
            path,
            headers={"Host": f"127.0.0.1:{port}", "Authorization": "Bearer " + token},
        )
        response = connection.getresponse()
        body = response.read(MAX_STATE + 1)
    except OSError as error:
        raise PanelError(f"control API unreachable: {error}") from error
    finally:
        connection.close()
    if len(body) > MAX_STATE:
        raise PanelError("control API answer is too large")
    try:
        return response.status, json.loads(body)
    except ValueError as error:
        raise PanelError(f"control API answered HTTP {response.status}") from error


def fetch_state(run_dir):
    status, value = fetch(run_dir, "/api/emu/state")
    if status != 200:
        raise PanelError(f"control API answered HTTP {status}")
    return value


def fetch_i2c(run_dir, since=None):
    """One page of the run's structured I2C history (--i2c-history), after an opaque cursor."""
    path = "/api/emu/i2c?limit=128"
    if since is not None:
        if not CURSOR.fullmatch(since):
            raise ValueError("invalid cursor")
        path += "&since=" + quote(since, safe="")
    status, value = fetch(run_dir, path)
    if status == 409 and isinstance(value, dict) and value.get("error") == "unsupported_capability":
        raise PanelError("the run has no I2C history; start it with --i2c-history")
    if status != 200:
        raise PanelError(f"control API answered HTTP {status}")
    return value


def read_serial(run_dir, offset=None):
    """New serial output after a byte offset. Without one, the last chunk of the log. A log
    shorter than the offset was replaced (a QEMU startup retry), so reading starts over; so does a
    missing one, which the retry unlinks before recreating it."""
    path = run_dir / "serial.log"
    try:
        with path.open("rb") as log:
            size = log.seek(0, 2)
            restarted = offset is not None and offset > size
            if offset is None or restarted:
                offset = max(0, size - SERIAL_CHUNK) if offset is None else 0
            log.seek(offset)
            data = log.read(SERIAL_CHUNK)
    except FileNotFoundError:
        return {"offset": 0, "text": "", "restarted": bool(offset), "available": False}
    except OSError as error:
        raise PanelError(f"serial log unreadable: {error}") from error
    # Keep a multibyte character split by the chunk end for the next read.
    try:
        text = data.decode()
    except UnicodeDecodeError as error:
        if error.reason == "unexpected end of data":
            data = data[: error.start]
        text = data.decode(errors="replace")
    return {"offset": offset + len(data), "text": text, "restarted": restarted, "available": True}


def run_links(run_dir):
    """What the page may link to: the firmware's own web UI, when the run records one."""
    endpoint = manifest_of(run_dir).get("endpoint")
    return {"firmware_url": endpoint if loopback_port(endpoint) else None}


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, run_dir, port=0):
        self.run_dir = Path(run_dir)
        template = files("flapsim").joinpath("data/panel/index.html").read_text(encoding="utf-8")
        self.page = assemble(template).encode()
        super().__init__(("127.0.0.1", port), PanelHandler)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}/"


class PanelHandler(BaseHTTPRequestHandler):
    server_version = "flapsim-panel"
    sys_version = ""

    def log_message(self, *args):
        pass

    def send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status, value):
        self.send(status, json.dumps(value).encode(), "application/json")

    def do_GET(self):
        # Same DNS-rebinding and cross-site defence as the control API itself.
        if not accepts_authority(
            self.headers.get_all("Host") or [],
            self.headers.get_all("Origin") or [],
            self.server.server_address[1],
        ):
            self.send_json(403, {"error": "forbidden_origin_or_host"})
            return
        url = urlsplit(self.path)
        try:
            query = parse_qs(url.query, keep_blank_values=True, strict_parsing=bool(url.query))
        except ValueError:
            query = None
        routes = {
            "/": (set(), lambda: None),
            "/state": (set(), lambda: fetch_state(self.server.run_dir)),
            "/run": (set(), lambda: run_links(self.server.run_dir)),
            "/serial": ({"since"}, lambda: read_serial(self.server.run_dir, since_offset(query))),
            "/i2c": ({"since"}, lambda: fetch_i2c(self.server.run_dir, since_cursor(query))),
        }
        if url.path not in routes:
            self.send_json(404, {"error": "not_found"})
            return
        allowed, handler = routes[url.path]
        if query is None or set(query) - allowed or any(len(v) != 1 for v in query.values()):
            self.send_json(400, {"error": "invalid_query"})
            return
        if url.path == "/":
            self.send(200, self.server.page, "text/html; charset=utf-8")
            return
        try:
            self.send_json(200, handler())
        except ValueError:
            self.send_json(400, {"error": "invalid_query"})
        except PanelError as error:
            self.send_json(503, {"error": str(error)})

    do_HEAD = do_GET


def since_offset(query):
    if "since" not in query:
        return None
    value = query["since"][0]
    if not value.isascii() or not value.isdigit() or len(value) > 18:
        raise ValueError("invalid serial offset")
    return int(value)


def since_cursor(query):
    return query["since"][0] if "since" in query else None


def serve(run_dir, port, ready=None):
    """Serve until interrupted; `ready` receives the URL once the socket is listening."""
    server = PanelServer(run_dir, port)
    try:
        (ready or print)(f"Panel for {run_dir}: {server.url}")
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()


def start(run_dir, port=0):
    """A background panel, for tests: returns (server, thread)."""
    server = PanelServer(run_dir, port)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    return server, thread
