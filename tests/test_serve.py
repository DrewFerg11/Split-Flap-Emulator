"""`flapsim serve`: a supervised hold run, the panel for whichever run is current, and firmware
loading. The emulator child is the controlled fake QEMU, never firmware verification."""

import http.client
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from flapsim.executor import HoldExecutor, load_executor
from flapsim.hardware import load_hardware
from flapsim.panel import PanelServer
from flapsim.serve import ServeError, Supervisor, check_image, download_release

IMAGE = b"\xff" * 0x1000 + b"\xe9" + b"\x00" * 100


def image(marker):
    return IMAGE + marker


def test_hold_scenario_runs_until_interrupted():
    executor = load_executor("hold", load_hardware("esp32-8-single"))
    assert isinstance(executor, HoldExecutor) and executor.document["mode"] == "hold"
    with pytest.raises(ValueError, match="step-based scenario"):
        load_executor("hold", load_hardware("esp32-8-single"), variables=["A=1"])

    class Context:
        pauses = 0

        def pause(self):
            self.pauses += 1
            if self.pauses == 3:
                raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        executor.execute(Context())


@pytest.mark.parametrize(
    "data, message",
    [
        (b"", "larger than 4 KiB"),
        (IMAGE[:0x1000], "larger than 4 KiB"),
        (b"\xff" * 0x1000 + b"\x00" * 10, "no bootloader"),
        (IMAGE + b"\x00" * (4 * 1024 * 1024), "at most"),
    ],
)
def test_only_esp32_factory_images_load(data, message):
    with pytest.raises(ServeError, match=message):
        check_image(data)
    check_image(IMAGE)


@pytest.mark.parametrize("tag", ["latest", "v1", "1.4.1", "v1.4.1/../x", "v1.4.1-", "v1.4.1 "])
def test_release_tags_are_validated_before_any_download(monkeypatch, tag):
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("downloaded"))
    with pytest.raises(ServeError, match="release tag"):
        download_release(tag)


def test_release_download_uses_the_tags_factory_asset(monkeypatch):
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return IMAGE

    def urlopen(url, timeout):
        seen.append(url)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert download_release("v1.5.0-rc.1") == IMAGE
    assert seen == [
        "https://github.com/DrewFerg11/Split-Flap-Display/releases/download/"
        "v1.5.0-rc.1/splitflap-esp32_n4-v1.5.0-rc.1-factory.bin"
    ]

    def offline(url, timeout):
        raise OSError("network unreachable")

    monkeypatch.setattr("urllib.request.urlopen", offline)
    with pytest.raises(ServeError, match="cannot download v1.4.1"):
        download_release("v1.4.1")


def wait_for_control(run_dir, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            control = json.loads((run_dir / "run.json").read_text()).get("control", {})
        except (OSError, ValueError):
            control = {}
        if control.get("endpoint") and control.get("status") != "stopped":
            return control
        time.sleep(0.1)
    raise AssertionError(f"no control API for {run_dir}")


@pytest.fixture
def supervisor(tmp_path, runtime):
    first = tmp_path / "first.bin"
    first.write_bytes(image(b"first"))
    out = tmp_path / "serve"
    out.mkdir()
    supervisor = Supervisor(
        out,
        ["--mode", "qemu", "--hardware", "esp32-8-single", "--qemu-prefix", str(runtime)],
        "qemu",
        first,
    )
    supervisor.start()
    try:
        yield supervisor
    finally:
        supervisor.stop()


def manifest(run_dir):
    return json.loads((run_dir / "run.json").read_text())


def test_supervisor_restarts_the_run_with_new_firmware(supervisor):
    wait_for_control(supervisor.run_dir)
    first = supervisor.run_dir
    assert manifest(first)["scenario"] == {"version": 1, "mode": "hold"}
    described = supervisor.load(image(b"second"), "second.bin")
    assert described["run"] == "run-2" and described["running"]
    assert described["firmware"]["name"] == "second.bin"
    # The previous run was stopped cleanly and kept its evidence.
    assert manifest(first)["error"] == "interrupted by SIGINT"
    wait_for_control(supervisor.run_dir)
    stored = supervisor.out / "firmware" / f"{described['firmware']['sha256']}.bin"
    assert stored.read_bytes() == image(b"second")
    assert Path(manifest(supervisor.run_dir)["firmware"]["path"]) == stored
    assert supervisor.restart()["run"] == "run-3"
    with pytest.raises(ServeError, match="no bootloader"):
        supervisor.load(b"\xff" * 0x2000, "bad.bin")
    assert supervisor.describe()["run"] == "run-3"


def test_native_serve_does_not_load_images(tmp_path):
    supervisor = Supervisor(tmp_path, [], "native")
    with pytest.raises(ServeError, match="QEMU mode"):
        supervisor.load(IMAGE, "x.bin")


@pytest.fixture
def served(supervisor):
    server = PanelServer(None, 0, controls=True, supervisor=supervisor, hosts=["flap.lan:8765"])
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def call(server, method, path, body=b"", host=None, content_type="application/json", **headers):
    port = server.server_address[1]
    host = host or f"127.0.0.1:{port}"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    sent = {"Host": host, **headers}
    if method == "POST":
        sent.update(
            {
                "Origin": f"http://{host}",
                "Content-Type": content_type,
                "X-Flapsim-Panel-Key": server.key,
            }
        )
    connection.request(method, path, body, sent)
    response = connection.getresponse()
    answer = json.loads(response.read() or b"null")
    connection.close()
    return response.status, answer


def test_panel_follows_the_served_run_and_loads_firmware(served, monkeypatch):
    supervisor = served.supervisor
    wait_for_control(supervisor.run_dir)
    status, info = call(served, "GET", "/run")
    assert status == 200 and info["controls"] and info["serve"]["run"] == "run-1"
    status, answer = call(
        served,
        "POST",
        "/control/firmware",
        image(b"upload"),
        content_type="application/octet-stream",
        **{"X-Flapsim-Firmware-Name": "my build.bin"},
    )
    assert status == 200 and answer["run"] == "run-2"
    assert answer["firmware"]["name"] == "my build.bin"
    assert served.run_dir == supervisor.run_dir == supervisor.out / "run-2"
    monkeypatch.setattr("flapsim.serve.download_release", lambda tag: image(tag.encode()))
    status, answer = call(served, "POST", "/control/firmware-release", b'{"tag":"v1.4.1"}')
    assert status == 200 and answer["firmware"]["name"] == "v1.4.1 release factory image"
    status, answer = call(served, "POST", "/control/restart", b"{}")
    assert status == 200 and answer["run"] == "run-4"


@pytest.mark.parametrize(
    "path, body, content_type, headers, status",
    [
        ("/control/firmware", b"\xff" * 0x2000, "application/octet-stream", {}, 422),
        ("/control/firmware", IMAGE, "application/json", {}, 415),
        (
            "/control/firmware",
            IMAGE,
            "application/octet-stream",
            {"X-Flapsim-Firmware-Name": "badé"},
            400,
        ),
        ("/control/firmware-release", b'{"tag":"v1.4.1","x":1}', "application/json", {}, 400),
        ("/control/firmware-release", b'{"tag":"latest"}', "application/json", {}, 422),
        ("/control/restart", b'{"now":true}', "application/json", {}, 400),
    ],
)
def test_serve_commands_are_validated(served, path, body, content_type, headers, status):
    wait_for_control(served.supervisor.run_dir)
    assert call(served, "POST", path, body, content_type=content_type, **headers)[0] == status
    assert served.supervisor.describe()["run"] == "run-1"


def test_oversized_images_are_refused_before_reading(served):
    port = served.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.putrequest("POST", "/control/firmware", skip_host=True)
    for name, value in {
        "Host": f"127.0.0.1:{port}",
        "Origin": f"http://127.0.0.1:{port}",
        "Content-Type": "application/octet-stream",
        "Content-Length": str(4 * 1024 * 1024 + 1),
        "X-Flapsim-Panel-Key": served.key,
    }.items():
        connection.putheader(name, value)
    connection.endheaders()
    assert connection.getresponse().status == 413
    connection.close()


def test_allowed_hosts_extend_the_names_the_panel_answers(served):
    wait_for_control(served.supervisor.run_dir)
    assert call(served, "GET", "/run", host="flap.lan:8765")[0] == 200
    assert call(served, "GET", "/run", host="other.lan:8765")[0] == 403
    assert call(served, "POST", "/control/restart", b"{}", host="flap.lan:8765")[0] == 200


def test_plain_panel_has_no_serve_routes(tmp_path):
    from flapsim.panel import start

    server, thread = start(tmp_path, controls=True)
    try:
        status, _ = call(server, "POST", "/control/restart", b"{}")
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize(
    "arguments, message",
    [
        (["--hardware", "esp32-8-single"], "needs --firmware or --firmware-release"),
        (["--hardware", "x", "--mode", "native"], "needs --native-build or --firmware-src"),
        (["--hardware", "x", "--mode", "native", "--firmware", "a.bin"], "from source"),
        (["--hardware", "x", "--native-build", "b"], "native mode options"),
        (["--hardware", "x", "--firmware", "a.bin", "--allow-host", "flap.lan"], "HOST:PORT"),
        (["--hardware", "x", "--firmware", "a.bin", "--port", "70000"], "--port"),
    ],
)
def test_serve_arguments_are_checked_before_starting(arguments, message):
    result = subprocess.run(
        [sys.executable, "-m", "flapsim.cli", "serve", *arguments],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2 and message in result.stderr
