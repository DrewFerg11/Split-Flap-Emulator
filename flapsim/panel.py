"""Live display panel for a running emulator (#21).

The control API (#20) rejects browser origins and needs a bearer token, so a page can't
call it directly. This small loopback server serves the panel page and fetches state and
I2C history for it server-side, using the run's own token file, and tails the run's serial
log. It is read-only unless started with controls: then it also forwards reset, power and
module mechanics/fault commands, but only from a page that holds the panel's own key.
"""

import hmac
import http.client
import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from flapsim.control import accepts_authority
from flapsim.ui import assemble

STATE_TIMEOUT = 2.0
# The control API asks clients to allow seven seconds for a reset and twelve for power.
COMMAND_TIMEOUT = 15.0
MAX_COMMAND = 2048
KEY_HEADER = "X-Flapsim-Panel-Key"
MAX_STATE = 1024 * 1024
SERIAL_CHUNK = 64 * 1024
# A reader further behind than this skips to the newest output instead of chasing a flood.
SERIAL_BACKLOG = 4 * SERIAL_CHUNK
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


def fetch(run_dir, path, command=None):
    """GET a control API path, or POST a command to it, with the run's token:
    (HTTP status, decoded JSON body)."""
    port, token = control_target(run_dir)
    timeout = STATE_TIMEOUT if command is None else COMMAND_TIMEOUT
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Host": f"127.0.0.1:{port}", "Authorization": "Bearer " + token}
    try:
        if command is None:
            connection.request("GET", path, headers=headers)
        else:
            headers["Content-Type"] = "application/json"
            connection.request("POST", path, json.dumps(command).encode(), headers)
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
    missing one, which the retry unlinks before recreating it. A reader more than SERIAL_BACKLOG
    behind skips to the last chunk; `skipped` counts the bytes it did not see (serial.log keeps
    them)."""
    path = run_dir / "serial.log"
    skipped = 0
    try:
        with path.open("rb") as log:
            size = log.seek(0, 2)
            restarted = offset is not None and offset > size
            if offset is None or restarted:
                offset = max(0, size - SERIAL_CHUNK) if offset is None else 0
            elif size - offset > SERIAL_BACKLOG:
                skipped = size - SERIAL_CHUNK - offset
                offset = size - SERIAL_CHUNK
            log.seek(offset)
            data = log.read(SERIAL_CHUNK)
    except FileNotFoundError:
        return {
            "offset": 0,
            "text": "",
            "restarted": bool(offset),
            "skipped": 0,
            "available": False,
        }
    except OSError as error:
        raise PanelError(f"serial log unreadable: {error}") from error
    # Keep a multibyte character split by the chunk end for the next read.
    try:
        text = data.decode()
    except UnicodeDecodeError as error:
        if error.reason == "unexpected end of data":
            data = data[: error.start]
        text = data.decode(errors="replace")
    return {
        "offset": offset + len(data),
        "text": text,
        "restarted": restarted,
        "skipped": skipped,
        "available": True,
    }


def run_links(run_dir, controls=False):
    """What the page may link to (the firmware's own web UI, when the run records one) and
    whether this panel forwards commands."""
    endpoint = manifest_of(run_dir).get("endpoint")
    return {"firmware_url": endpoint if loopback_port(endpoint) else None, "controls": controls}


class CommandIds:
    """Command IDs for the control API, which needs them strictly increasing per kind across
    applied commands. Milliseconds since the epoch stay ahead of scripted clients' small IDs
    and of an earlier panel for the same run."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last = 0

    def next(self):
        with self.lock:
            self.last = max(self.last + 1, time.time_ns() // 1_000_000)
            return self.last


def is_int(value):
    return type(value) is int


def command(run_dir, ids, route, body):
    """Forward one panel command: (control API status, its JSON answer)."""
    if not isinstance(body, dict):
        raise ValueError("the command must be a JSON object")
    if route == "/control/reset":
        if body:
            raise ValueError("reset takes no fields")
        return fetch(run_dir, "/api/emu/reset", {"id": ids.next()})
    if route == "/control/power":
        if set(body) != {"on"} or type(body["on"]) is not bool:
            raise ValueError('power takes exactly {"on": true|false}')
        return fetch(run_dir, "/api/emu/power", {"id": ids.next(), "on": body["on"]})
    if (
        set(body) != {"bus", "addr", "patch"}
        or not is_int(body["bus"])
        or not is_int(body["addr"])
        or not 0 <= body["bus"] <= 1
        or not 0 <= body["addr"] <= 127
        or not isinstance(body["patch"], dict)
        or not body["patch"]
    ):
        raise ValueError("mechanics take bus, addr and a nonempty patch")
    # The control API validates the patch itself; the panel supplies the current revision.
    revision = fetch_state(run_dir)["state"].get("mechanics_revision", 0)
    path = f"/api/emu/modules/{body['bus']}/{body['addr']}/mech"
    command = {"id": ids.next(), "expected_revision": revision, "patch": body["patch"]}
    return fetch(run_dir, path, command)


def strict_json(data):
    def unique(pairs):
        keys = [k for k, _ in pairs]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate JSON key")
        return dict(pairs)

    def constant(name):
        raise ValueError(f"{name} is not valid JSON")

    return json.loads(data, object_pairs_hook=unique, parse_constant=constant)


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, run_dir, port=0, controls=False):
        self.run_dir = Path(run_dir)
        # Only a page opened from the printed URL learns the key: it is in the fragment,
        # which browsers never send, so another local client can't read it from the panel.
        self.key = secrets.token_urlsafe(24) if controls else None
        self.ids = CommandIds()
        template = files("flapsim").joinpath("data/panel/index.html").read_text(encoding="utf-8")
        self.page = assemble(template).encode()
        super().__init__(("127.0.0.1", port), PanelHandler)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}/"

    @property
    def page_url(self):
        """The URL to open: with controls, it carries the key in its fragment."""
        return self.url + (f"#key={self.key}" if self.key else "")


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
            "/run": (set(), lambda: run_links(self.server.run_dir, self.server.key is not None)),
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

    def do_POST(self):
        if self.server.key is None:
            self.send_json(501, {"error": "read_only_panel"})
            return
        port = self.server.server_address[1]
        origins = self.headers.get_all("Origin") or []
        # A browser always sends Origin with a POST; anything else is not the panel's page.
        if not origins or not accepts_authority(self.headers.get_all("Host") or [], origins, port):
            self.send_json(403, {"error": "forbidden_origin_or_host"})
            return
        keys = self.headers.get_all(KEY_HEADER) or []
        if len(keys) != 1 or not hmac.compare_digest(keys[0].encode(), self.server.key.encode()):
            self.send_json(403, {"error": "forbidden_panel_key"})
            return
        if self.path not in {"/control/reset", "/control/power", "/control/mech"}:
            self.send_json(404, {"error": "not_found"})
            return
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self.send_json(415, {"error": "unsupported_media_type"})
            return
        length = self.headers.get("Content-Length")
        if length is None or not length.isascii() or not length.isdigit():
            self.send_json(411, {"error": "length_required"})
            return
        if int(length) > MAX_COMMAND:
            self.send_json(413, {"error": "body_too_large"})
            return
        self.connection.settimeout(1.0)
        try:
            body = strict_json(self.rfile.read(int(length)))
            status, answer = command(self.server.run_dir, self.server.ids, self.path, body)
        except TimeoutError:
            self.send_json(408, {"error": "body_timeout"})
        except ValueError as error:
            self.send_json(400, {"error": "invalid_command", "detail": str(error)})
        except RecursionError:
            self.send_json(
                400, {"error": "invalid_command", "detail": "the command nests too deeply"}
            )
        except PanelError as error:
            self.send_json(503, {"error": str(error)})
        else:
            self.send_json(status, answer)


def since_offset(query):
    if "since" not in query:
        return None
    value = query["since"][0]
    if not value.isascii() or not value.isdigit() or len(value) > 18:
        raise ValueError("invalid serial offset")
    return int(value)


def since_cursor(query):
    return query["since"][0] if "since" in query else None


def serve(run_dir, port, ready=None, controls=False):
    """Serve until interrupted; `ready` receives the URL once the socket is listening."""
    server = PanelServer(run_dir, port, controls)
    try:
        (ready or print)(f"Panel for {run_dir}: {server.page_url}")
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()


def start(run_dir, port=0, controls=False):
    """A background panel, for tests: returns (server, thread)."""
    server = PanelServer(run_dir, port, controls)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    return server, thread
