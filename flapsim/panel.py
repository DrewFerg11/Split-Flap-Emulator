"""Read-only live display panel for a running emulator (#21, first slice).

The control API (#20) rejects browser origins and needs a bearer token, so a page can't
call it directly. This small loopback server serves the panel page and fetches state
for it server-side, using the run's own token file. It never forwards mutations.
"""

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from flapsim.control import accepts_authority
from flapsim.ui import assemble

STATE_TIMEOUT = 2.0
MAX_STATE = 1024 * 1024


class PanelError(RuntimeError):
    """The run has no live control API to read (yet, or any more)."""


def control_target(run_dir):
    """The control endpoint and token recorded by a run started with --control-port."""
    try:
        manifest = json.loads((run_dir / "run.json").read_text())
    except (OSError, ValueError) as error:
        raise PanelError(f"no readable run.json in {run_dir}") from error
    control = manifest.get("control") if isinstance(manifest, dict) else None
    if not isinstance(control, dict) or not control.get("endpoint"):
        raise PanelError("the run has no control API; start it with --control-port 0")
    if control.get("status") == "stopped":
        raise PanelError("the run has finished; its control API is stopped")
    try:
        endpoint = urlsplit(control["endpoint"])
        port = endpoint.port  # raises ValueError for a non-numeric or out-of-range port
    except (TypeError, ValueError, AttributeError) as error:
        raise PanelError("the control endpoint is not a loopback HTTP address") from error
    if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or not port:
        raise PanelError("the control endpoint is not a loopback HTTP address")
    try:
        token = Path(control["token_file"]).read_text().strip()
    except (KeyError, TypeError, OSError) as error:
        raise PanelError("the run's control token file is not readable") from error
    return port, token


def fetch_state(run_dir):
    port, token = control_target(run_dir)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=STATE_TIMEOUT)
    try:
        connection.request(
            "GET",
            "/api/emu/state",
            headers={"Host": f"127.0.0.1:{port}", "Authorization": "Bearer " + token},
        )
        response = connection.getresponse()
        body = response.read(MAX_STATE + 1)
    except OSError as error:
        raise PanelError(f"control API unreachable: {error}") from error
    finally:
        connection.close()
    if response.status != 200 or len(body) > MAX_STATE:
        raise PanelError(f"control API answered HTTP {response.status}")
    try:
        return json.loads(body)
    except ValueError as error:
        raise PanelError("control API returned invalid JSON") from error


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
        elif self.path == "/":
            self.send(200, self.server.page, "text/html; charset=utf-8")
        elif self.path == "/state":
            try:
                self.send_json(200, fetch_state(self.server.run_dir))
            except PanelError as error:
                self.send_json(503, {"error": str(error)})
        else:
            self.send_json(404, {"error": "not_found"})

    do_HEAD = do_GET


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
