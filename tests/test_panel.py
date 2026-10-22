"""The read-only live panel proxies state with the run's token and never exposes more."""

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from flapsim.panel import start

TOKEN = "fixture-control-token"
STATE = {
    "api_version": 1,
    "run_id": "run",
    "backend": "native",
    "boot_generation": 0,
    "live": True,
    "state": {"guest_ns": 1, "text": "A", "modules": []},
}


@pytest.fixture
def control():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Host"), self.headers.get("Authorization")))
            port = self.server.server_address[1]
            ok = (
                self.path == "/api/emu/state"
                and self.headers.get("Host") == f"127.0.0.1:{port}"
                and self.headers.get("Authorization") == "Bearer " + TOKEN
            )
            body = json.dumps(STATE if ok else {"error": "unauthorized"}).encode()
            self.send_response(200 if ok else 401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], seen
    finally:
        server.shutdown()
        server.server_close()


def write_run(out, port, token=TOKEN, **control):
    (out / "control-token").write_text(token + "\n")
    control = {
        "endpoint": f"http://127.0.0.1:{port}",
        "token_file": str(out / "control-token"),
        **control,
    }
    (out / "run.json").write_text(json.dumps({"control": control}))


@pytest.fixture
def panel(tmp_path):
    server, thread = start(tmp_path)
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def request(port, path, method="GET", host=None, origin=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if origin:
        headers["Origin"] = origin
    connection.request(method, path, headers=headers)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response, body


def test_page_and_live_state_use_the_runs_token(tmp_path, control, panel):
    port, seen = control
    write_run(tmp_path, port)
    response, body = request(panel, "/")
    assert response.status == 200 and b"Split-Flap Emulator" in body
    assert "default-src 'none'" in response.getheader("Content-Security-Policy")
    response, body = request(panel, "/state")
    assert response.status == 200 and json.loads(body) == STATE
    assert response.getheader("Cache-Control") == "no-store"
    assert seen == [("/api/emu/state", f"127.0.0.1:{port}", "Bearer " + TOKEN)]
    # The token never reaches the browser.
    assert TOKEN.encode() not in body


@pytest.mark.parametrize(
    "setup, message",
    [
        (lambda out, port: None, "no readable run.json"),
        (lambda out, port: (out / "run.json").write_text("{}"), "no control API"),
        (lambda out, port: write_run(out, port, status="stopped"), "has finished"),
        (lambda out, port: write_run(out, port, token="wrong"), "HTTP 401"),
        (
            lambda out, port: write_run(out, port, endpoint="http://example.com:1"),
            "not a loopback",
        ),
    ],
)
def test_unavailable_state_is_reported_not_guessed(tmp_path, control, panel, setup, message):
    setup(tmp_path, control[0])
    response, body = request(panel, "/state")
    assert response.status == 503 and message in json.loads(body)["error"]


@pytest.mark.parametrize(
    "host, origin", [("evil.example", None), (None, "http://evil.example"), ("localhost", None)]
)
def test_foreign_host_or_origin_is_rejected(tmp_path, control, panel, host, origin):
    write_run(tmp_path, control[0])
    response, _ = request(panel, "/state", host=host, origin=origin)
    assert response.status == 403
    assert not control[1]


def test_panel_is_read_only(tmp_path, control, panel):
    write_run(tmp_path, control[0])
    assert request(panel, "/api/emu/reset", method="POST")[0].status == 501
    assert request(panel, "/api/emu/state")[0].status == 404
    assert not control[1]
