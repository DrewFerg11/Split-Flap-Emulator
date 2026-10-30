"""The read-only live panel proxies state with the run's token and never exposes more."""

import http.client
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from flapsim import panel as panel_module
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


I2C_PAGE = {"api_version": 1, "records": [], "next": "run:7", "latest": "run:7", "more": False}
# Control API answers for paths other than the state route: (status, JSON body).
I2C = {}
COMMANDS = {}


@pytest.fixture
def control():
    seen = []
    I2C.clear()
    COMMANDS.clear()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Host"), self.headers.get("Authorization")))
            port = self.server.server_address[1]
            authorized = (
                self.headers.get("Host") == f"127.0.0.1:{port}"
                and self.headers.get("Authorization") == "Bearer " + TOKEN
            )
            status, value = 404, {"error": "not_found"}
            if not authorized:
                status, value = 401, {"error": "unauthorized"}
            elif self.path == "/api/emu/state":
                status, value = 200, STATE
            elif self.path in I2C:
                status, value = I2C[self.path]
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, self.headers.get("Authorization"), body))
            status, value = COMMANDS.get(self.path, (404, {"error": "not_found"}))
            encoded = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

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
        (
            lambda out, port: write_run(out, port, endpoint="http://127.0.0.1:99999"),
            "not a loopback",
        ),
        (
            lambda out, port: write_run(out, port, endpoint="http://127.0.0.1:nope"),
            "not a loopback",
        ),
    ],
)
def test_unavailable_state_is_reported_not_guessed(tmp_path, control, panel, setup, message):
    setup(tmp_path, control[0])
    response, body = request(panel, "/state")
    assert response.status == 503 and message in json.loads(body)["error"]


@pytest.mark.parametrize(
    "host, origin",
    [
        ("evil.example", None),
        (None, "http://evil.example"),
        ("localhost", None),
        # Compared raw, like the control API: whitespace is never normalized away.
        ("{authority} ", None),
        ("{authority} foo", None),
        ("{authority}\t", None),
    ],
)
def test_foreign_host_or_origin_is_rejected(tmp_path, control, panel, host, origin):
    write_run(tmp_path, control[0])
    if host:
        host = host.format(authority=f"127.0.0.1:{panel}")
    response, _ = request(panel, "/state", host=host, origin=origin)
    assert response.status == 403
    assert not control[1]


def test_panel_is_read_only(tmp_path, control, panel):
    write_run(tmp_path, control[0])
    assert request(panel, "/api/emu/reset", method="POST")[0].status == 501
    assert request(panel, "/api/emu/state")[0].status == 404
    assert not control[1]


def test_panel_and_replay_share_one_self_contained_style(tmp_path, control, panel):
    from flapsim.report import write_replay
    from flapsim.ui import assemble

    write_run(tmp_path, control[0])
    page = request(panel, "/")[1].decode()
    write_replay(tmp_path, [], None)
    replay = (tmp_path / "replay.html").read_text()
    for document in (page, replay):
        assert "@SHARED_" not in document and "@@" not in document
        assert "--bg: #15171a" in document and "const FlapUI" in document
        # Self-contained: no stylesheet, script, image or font is fetched from anywhere.
        assert not re.search(
            r"<link|<img|src=|@import|url\(|https?://(?!www\.w3\.org/2000/svg)", document
        )
    assert assemble("/*@SHARED_CSS@*/|/*@SHARED_JS@*/").count("FlapUI") > 0


def test_serial_console_tails_the_runs_log(tmp_path, panel):
    response, body = request(panel, "/serial")
    assert response.status == 200
    assert json.loads(body) == {
        "offset": 0,
        "text": "",
        "restarted": False,
        "skipped": 0,
        "available": False,
    }
    log = tmp_path / "serial.log"
    log.write_bytes(b"x" * 70_000 + b"boot\n")
    first = json.loads(request(panel, "/serial")[1])
    # Without an offset, only the last chunk: a long log is not sent whole.
    assert first["offset"] == 70_005 and first["text"].endswith("boot\n")
    assert len(first["text"]) == 64 * 1024
    with log.open("ab") as out:
        out.write("Homing \u00b0\n".encode())
    after = json.loads(request(panel, f"/serial?since={first['offset']}")[1])
    assert after["text"] == "Homing \u00b0\n" and not after["restarted"]
    # A shorter log was replaced by a startup retry: start over.
    log.write_bytes(b"retry\n")
    again = json.loads(request(panel, f"/serial?since={after['offset']}")[1])
    assert again == {
        "offset": 6,
        "text": "retry\n",
        "restarted": True,
        "skipped": 0,
        "available": True,
    }
    # The retry unlinks the log before recreating it: a poll in the gap clears the old text too.
    log.unlink()
    gap = json.loads(request(panel, f"/serial?since={again['offset']}")[1])
    assert gap == {"offset": 0, "text": "", "restarted": True, "skipped": 0, "available": False}
    assert not json.loads(request(panel, "/serial?since=0")[1])["restarted"]


def test_serial_console_skips_a_flood_instead_of_chasing_it(tmp_path, panel):
    log = tmp_path / "serial.log"
    log.write_bytes(b"a" * 1000)
    assert json.loads(request(panel, "/serial?since=0")[1])["skipped"] == 0
    with log.open("ab") as out:
        out.write(b"b" * (300 * 1024) + b"tail\n")
    value = json.loads(request(panel, "/serial?since=1000")[1])
    size = 1000 + 300 * 1024 + 5
    assert value["skipped"] == size - 64 * 1024 - 1000
    assert value["offset"] == size and value["text"].endswith("tail\n")
    # Up to four chunks behind is still read in full, chunk by chunk.
    with log.open("ab") as out:
        out.write(b"c" * (200 * 1024))
    assert json.loads(request(panel, f"/serial?since={size}")[1])["skipped"] == 0


def test_serial_console_keeps_a_split_character_for_the_next_read(tmp_path, panel):
    degree = "\u00b0".encode()
    (tmp_path / "serial.log").write_bytes(b"a" * (64 * 1024 - 1) + degree + b"z")
    first = json.loads(request(panel, "/serial?since=0")[1])
    assert first["offset"] == 64 * 1024 - 1 and "\ufffd" not in first["text"]
    second = json.loads(request(panel, f"/serial?since={first['offset']}")[1])
    assert second["text"] == "\u00b0z"


@pytest.mark.parametrize(
    "path",
    ["/serial?since=-1", "/serial?since=abc", "/serial?since=1&since=2", "/serial?x=1",
     "/serial?since=" + "9" * 19, "/i2c?since=a%26b", "/i2c?since=", "/state?x=1", "/?x"],
)  # fmt: skip
def test_panel_queries_are_validated(tmp_path, control, panel, path):
    write_run(tmp_path, control[0])
    response, body = request(panel, path)
    assert response.status == 400 and json.loads(body) == {"error": "invalid_query"}
    assert not control[1]


def test_i2c_monitor_pages_history_with_the_runs_token(tmp_path, control, panel):
    port, seen = control
    write_run(tmp_path, port)
    I2C["/api/emu/i2c?limit=128"] = (200, I2C_PAGE)
    I2C["/api/emu/i2c?limit=128&since=run%3A7"] = (200, {**I2C_PAGE, "records": [{"x": 1}]})
    response, body = request(panel, "/i2c")
    assert response.status == 200 and json.loads(body) == I2C_PAGE
    response, body = request(panel, "/i2c?since=run:7")
    assert json.loads(body)["records"] == [{"x": 1}]
    assert [path for path, _, auth in seen if auth == "Bearer " + TOKEN] == [
        "/api/emu/i2c?limit=128",
        "/api/emu/i2c?limit=128&since=run%3A7",
    ]
    assert TOKEN.encode() not in body


@pytest.mark.parametrize(
    "answer, message",
    [
        ((409, {"error": "unsupported_capability"}), "start it with --i2c-history"),
        ((409, {"error": "future_cursor"}), "HTTP 409"),
        ((503, {"error": "journal failed"}), "HTTP 503"),
    ],
)
def test_i2c_monitor_reports_unavailable_history(tmp_path, control, panel, answer, message):
    write_run(tmp_path, control[0])
    I2C["/api/emu/i2c?limit=128"] = answer
    response, body = request(panel, "/i2c")
    assert response.status == 503 and message in json.loads(body)["error"]


@pytest.mark.parametrize(
    "endpoint, url",
    [
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
        ("http://example.com:8080", None),
        ("javascript:alert(1)", None),
        ("http://127.0.0.1:8080/x?y", None),
        (None, None),
    ],
)
def test_firmware_link_is_only_the_runs_loopback_web_ui(tmp_path, control, panel, endpoint, url):
    write_run(tmp_path, control[0])
    manifest = json.loads((tmp_path / "run.json").read_text())
    (tmp_path / "run.json").write_text(json.dumps({**manifest, "endpoint": endpoint}))
    response, body = request(panel, "/run")
    assert response.status == 200
    assert json.loads(body) == {"firmware_url": url, "controls": False}


def test_page_renders_firmware_output_as_text_only(tmp_path, control, panel):
    write_run(tmp_path, control[0])
    page = request(panel, "/")[1].decode()
    for section in ['id="serial"', 'id="i2c"', 'id="firmware"']:
        assert section in page
    # Serial and I2C data come from the firmware: never parsed as HTML.
    assert "innerHTML" not in page and "insertAdjacentHTML" not in page


@pytest.fixture
def controls_panel(tmp_path):
    server, thread = start(tmp_path, controls=True)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def post(server, path, body, *, key=None, origin="own", content_type="application/json"):
    port = server.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    headers = {"Host": f"127.0.0.1:{port}", "Content-Type": content_type}
    if origin:
        headers["Origin"] = f"http://127.0.0.1:{port}" if origin == "own" else origin
    if key is not False:
        headers["X-Flapsim-Panel-Key"] = server.key if key is None else key
    connection.request("POST", path, data, headers)
    response = connection.getresponse()
    answer = json.loads(response.read())
    connection.close()
    return response.status, answer


def commands_sent(seen):
    return [entry for entry in seen if len(entry) == 3 and isinstance(entry[2], dict)]


def test_controls_forward_reset_and_power_with_increasing_ids(tmp_path, control, controls_panel):
    port, seen = control
    write_run(tmp_path, port)
    COMMANDS["/api/emu/reset"] = (202, {"status": "applied", "boot_generation": 1})
    COMMANDS["/api/emu/power"] = (202, {"status": "applied", "power_state": "off"})
    assert post(controls_panel, "/control/reset", {}) == (
        202,
        {"status": "applied", "boot_generation": 1},
    )
    status, answer = post(controls_panel, "/control/power", {"on": False})
    assert status == 202 and answer["power_state"] == "off"
    sent = commands_sent(seen)
    assert [path for path, _, _ in sent] == ["/api/emu/reset", "/api/emu/power"]
    assert all(auth == "Bearer " + TOKEN for _, auth, _ in sent)
    reset, power = sent[0][2], sent[1][2]
    assert set(reset) == {"id"} and power == {"id": power["id"], "on": False}
    assert power["id"] > reset["id"] > 1_600_000_000_000  # milliseconds since the epoch
    # The control API's answer passes through, errors included.
    COMMANDS["/api/emu/power"] = (409, {"error": "stale_or_reused_id"})
    assert post(controls_panel, "/control/power", {"on": True}) == (
        409,
        {"error": "stale_or_reused_id"},
    )


def test_controls_send_mechanics_with_the_current_revision(tmp_path, control, controls_panel):
    port, seen = control
    write_run(tmp_path, port)
    COMMANDS["/api/emu/modules/1/33/mech"] = (202, {"status": "applied"})
    patch = {"faults": {"sensor": "stuck_inactive"}}
    status, _ = post(controls_panel, "/control/mech", {"bus": 1, "addr": 33, "patch": patch})
    assert status == 202
    [(path, _, sent)] = commands_sent(seen)
    # STATE has no mechanics_revision: no edit yet, so the guard is zero.
    assert path == "/api/emu/modules/1/33/mech"
    assert sent == {"id": sent["id"], "expected_revision": 0, "patch": patch}


@pytest.mark.parametrize(
    "path, body",
    [
        ("/control/reset", {"id": 5}),
        ("/control/reset", []),
        ("/control/power", {"on": 1}),
        ("/control/power", {"on": True, "id": 9}),
        ("/control/mech", {"bus": 2, "addr": 33, "patch": {"x": 1}}),
        ("/control/mech", {"bus": True, "addr": 33, "patch": {"x": 1}}),
        ("/control/mech", {"bus": 0, "addr": 32, "patch": {}}),
        ("/control/mech", {"bus": 0, "addr": 32}),
        ("/control/reset", b'{"a":1,"a":2}'),
        ("/control/reset", b"not json"),
        ("/control/mech", b'{"bus":0,"addr":32,"patch":{"x":NaN}}'),
        ("/control/mech", b'{"bus":0,"addr":32,"patch":{"x":-Infinity}}'),
    ],
)
def test_controls_reject_malformed_commands(tmp_path, control, controls_panel, path, body):
    write_run(tmp_path, control[0])
    status, answer = post(controls_panel, path, body)
    assert status == 400 and answer["error"] == "invalid_command"
    assert not commands_sent(control[1])


def test_a_command_that_overflows_the_json_stack_is_a_bad_request(
    tmp_path, control, controls_panel, monkeypatch
):
    # Nesting that fits the 2 KiB cap can still exhaust the interpreter's recursion limit.
    def overflow(*args):
        raise RecursionError

    monkeypatch.setattr(panel_module, "command", overflow)
    write_run(tmp_path, control[0])
    status, answer = post(controls_panel, "/control/reset", {})
    assert status == 400 and answer["error"] == "invalid_command"
    assert not commands_sent(control[1])


@pytest.mark.parametrize(
    "options, status, error",
    [
        ({"key": False}, 403, "forbidden_panel_key"),
        ({"key": "wrong"}, 403, "forbidden_panel_key"),
        ({"origin": None}, 403, "forbidden_origin_or_host"),
        ({"origin": "http://evil.example"}, 403, "forbidden_origin_or_host"),
        ({"content_type": "text/plain"}, 415, "unsupported_media_type"),
    ],
)
def test_controls_accept_only_the_panels_own_page(
    tmp_path, control, controls_panel, options, status, error
):
    write_run(tmp_path, control[0])
    COMMANDS["/api/emu/reset"] = (202, {"status": "applied"})
    assert post(controls_panel, "/control/reset", {}, **options) == (status, {"error": error})
    assert not commands_sent(control[1])


def test_controls_reject_oversized_and_unknown_routes(tmp_path, control, controls_panel):
    write_run(tmp_path, control[0])
    assert post(controls_panel, "/control/reset", b"{}" + b" " * 2048)[0] == 413
    assert post(controls_panel, "/api/emu/reset", {})[0] == 404
    assert not commands_sent(control[1])


def test_controls_key_is_only_in_the_printed_url(tmp_path, control, controls_panel):
    write_run(tmp_path, control[0])
    port = controls_panel.server_address[1]
    assert controls_panel.page_url == f"http://127.0.0.1:{port}/#key={controls_panel.key}"
    for path in ["/", "/run", "/state", "/serial"]:
        assert controls_panel.key.encode() not in request(port, path)[1]
    assert json.loads(request(port, "/run")[1])["controls"] is True


def test_a_read_only_panel_never_forwards_commands(tmp_path, control, panel):
    write_run(tmp_path, control[0])
    connection = http.client.HTTPConnection("127.0.0.1", panel, timeout=5)
    connection.request(
        "POST",
        "/control/reset",
        b"{}",
        {"Host": f"127.0.0.1:{panel}", "Origin": f"http://127.0.0.1:{panel}",
         "Content-Type": "application/json"},
    )  # fmt: skip
    assert connection.getresponse().status == 501
    connection.close()
    assert not commands_sent(control[1])
