"""The verify tools' shared control-API client: the run's token, loopback, strict statuses."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from tools.control_client import ControlClient


@pytest.fixture
def run(tmp_path):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def answer(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            seen.append((self.command, self.path, self.headers["Authorization"], body))
            status = 409 if self.path.endswith("/busy") else 200
            encoded = json.dumps({"path": self.path}).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        do_GET = do_POST = answer

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    (tmp_path / "run.json").write_text(json.dumps({"control": {"endpoint": endpoint}}))
    (tmp_path / "control-token").write_text("secret\n")
    checkpoints = []
    context = SimpleNamespace(out=tmp_path, checkpoint=lambda: checkpoints.append(1))
    try:
        yield ControlClient(context, timeout=5), seen, checkpoints
    finally:
        server.shutdown()
        server.server_close()


def test_api_calls_use_the_runs_token_and_check_the_status(run, monkeypatch):
    # A configured HTTP proxy must never see loopback control traffic.
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    client, seen, checkpoints = run
    assert client.api() == {"path": "/api/emu/state"}
    assert client.api("/reset", {"id": 1}) == {"path": "/api/emu/reset"}
    assert client.call("/api/emu/busy", {"id": 2}) == (409, {"path": "/api/emu/busy"})
    with pytest.raises(RuntimeError, match="/busy: HTTP 409"):
        client.api("/busy")
    assert seen[:3] == [
        ("GET", "/api/emu/state", "Bearer secret", None),
        ("POST", "/api/emu/reset", "Bearer secret", {"id": 1}),
        ("POST", "/api/emu/busy", "Bearer secret", {"id": 2}),
    ]
    assert len(checkpoints) == 4  # every call honours the run's deadline first
