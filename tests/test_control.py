"""Real HTTP boundary and private channel failure semantics, without a QEMU build."""

import copy
import http.client
import json
import socket
import stat
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from flapsim.control import (
    ControlServer,
    SnapshotClient,
    Unavailable,
    accepts_authority,
    check_port,
)
from flapsim.hardware import load_hardware

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "port,hosts,origins,allowed",
    [
        (80, ["127.0.0.1"], [], True),
        (80, ["127.0.0.1:80"], [], True),
        (80, ["127.0.0.1"], ["http://127.0.0.1"], True),
        (80, ["127.0.0.1:80"], ["http://127.0.0.1"], True),
        (80, ["127.0.0.1"], ["http://127.0.0.1:80"], True),
        (8080, ["127.0.0.1:8080"], ["http://127.0.0.1:8080"], True),
        (8080, ["127.0.0.1"], [], False),
        (8080, ["127.0.0.1:80"], [], False),
        (80, ["127.0.0.1:8080"], [], False),
        (80, ["untrusted.example"], [], False),
        (80, ["127.0.0.1"], ["https://127.0.0.1"], False),
        (80, ["127.0.0.1"], ["http://untrusted.example"], False),
        (80, ["127.0.0.1"], ["null"], False),
        (80, [], [], False),
        (80, ["127.0.0.1", "127.0.0.1"], [], False),
        (80, ["127.0.0.1"], ["http://127.0.0.1", "http://127.0.0.1"], False),
    ],
)
def test_http_authority_default_port_and_rejection_cases(port, hosts, origins, allowed):
    assert accepts_authority(hosts, origins, port) is allowed


def recorded_snapshot():
    state = json.loads((ROOT / "tests/fixtures/golden-8-evidence.json").read_text())["states"][-1]
    hardware = load_hardware("esp32-8-single")
    for module, physical in zip(state["modules"], hardware["modules"], strict=True):
        module["coils"] = (module["latch"] >> 1) & 15
        module["mechanics"] = {
            k: physical[k]
            for k in [
                "steps_per_rotation",
                "magnet_position",
                "flap_offset",
                "sensor_window",
                "sensor_polarity",
            ]
        }
    state["guest_resets"] = 0
    return hardware, state


def reply_once(peer, response):
    with peer:
        assert peer.recv(64) == b"FLAPSIM_STATE/1"
        peer.sendall(response)


@pytest.mark.parametrize("failure", ["timeout", "truncated", "invalid", "forged", "eof"])
def test_channel_failure_is_bounded_and_does_not_reuse_a_late_reply(failure):
    hardware, state = recorded_snapshot()
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    client = SnapshotClient(parent, hardware)
    if failure == "forged":
        state["modules"][0]["char"] = "A"
    payload = {"truncated": b"x" * 70000, "invalid": b"[1]"}.get(
        failure, json.dumps(state).encode()
    )
    worker = None
    if failure == "eof":
        child.close()
    elif failure != "timeout":
        worker = threading.Thread(target=reply_once, args=(child, payload))
        worker.start()
    try:
        with pytest.raises(Unavailable):
            client.snapshot()
        with pytest.raises(Unavailable):
            client.snapshot()
    finally:
        client.close()
        child.close()
        if worker:
            worker.join(1)
            assert not worker.is_alive()


def request(server, path="/api/emu/state", *, authorized=True, headers=None, method="GET"):
    headers = dict(headers or {})
    if authorized:
        headers["Authorization"] = "Bearer " + server.token
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(
            urllib.request.Request(server.endpoint + path, headers=headers, method=method),
            timeout=2,
        )
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, response.headers, response.read()


def test_real_http_auth_origin_capabilities_and_shutdown(tmp_path):
    hardware, state = recorded_snapshot()
    server = ControlServer(0, tmp_path, hardware)
    parent, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    worker = threading.Thread(target=reply_once, args=(peer, json.dumps(state).encode()))
    worker.start()
    port = server.port
    try:
        assert stat.S_IMODE(server.token_path.stat().st_mode) == 0o600
        assert server.token not in json.dumps(server.metadata())
        assert request(server, authorized=False)[0] == 401
        assert request(server, headers={"Origin": "https://untrusted.example"})[0] == 403
        assert request(server, headers={"Host": "untrusted.example"})[0] == 403
        assert request(server)[0] == 503
        server.attach(parent)
        code, headers, body = request(server)
        assert code == 200
        assert headers["Cache-Control"] == "no-store"
        document = json.loads(body)
        assert document["backend"] == "qemu"
        assert document["capabilities"]["snapshot"] is True
        assert document["capabilities"]["set_mechanics"] is True
        assert not any(
            v
            for k, v in document["capabilities"].items()
            if k not in {"snapshot", "set_mechanics", "set_fault"}
        )
        assert document["boot_generation"] == 0
        assert document["state"]["text"] == state["text"]
        assert request(server, method="POST")[0] == 405
        assert request(server, path="/api/emu/reset", method="POST")[0] == 503
        server.detach()
        assert request(server)[0] == 503
    finally:
        server.close()
        parent.close()
        peer.close()
        worker.join(1)
    with socket.socket() as connection:
        assert connection.connect_ex(("127.0.0.1", port)) != 0
    assert not server.thread.is_alive()


def test_control_server_startup_failure_does_not_leak_listener(tmp_path):
    hardware, _ = recorded_snapshot()
    (tmp_path / "control-token").touch()
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    with pytest.raises(FileExistsError):
        ControlServer(port, tmp_path, hardware)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


def test_explicit_port_can_be_reused_after_server_closes_a_keepalive_connection(tmp_path):
    hardware, _ = recorded_snapshot()
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    server = ControlServer(0, first, hardware)
    port = server.port
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(
            "GET", "/api/emu/state", headers={"Authorization": "Bearer " + server.token}
        )
        response = connection.getresponse()
        assert response.status == 503
        response.read()
        # The server sends FIN first, while the client keeps its connection open.
        # Closing the client afterwards leaves the server port in TIME_WAIT.
        server.close()
        assert connection.sock.recv(1) == b""
    finally:
        connection.close()
        server.close()
    replacement = ControlServer(port, second, hardware)
    try:
        assert request(replacement)[0] == 503
    finally:
        replacement.close()


@pytest.mark.parametrize("port", [-1, 65536, True, 1.0])
def test_invalid_control_port(port):
    with pytest.raises(ValueError, match="control-port"):
        check_port(port)


def test_bad_mechanics_cannot_be_exposed_as_valid_live_state():
    hardware, state = recorded_snapshot()
    original = copy.deepcopy(state)
    state["modules"][0]["mechanics"]["sensor_polarity"] = "active-low"
    parent, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    worker = threading.Thread(target=reply_once, args=(peer, json.dumps(state).encode()))
    worker.start()
    client = SnapshotClient(parent, hardware)
    try:
        with pytest.raises(Unavailable):
            client.snapshot()
        assert (
            hardware["modules"][0]["sensor_polarity"]
            == original["modules"][0]["mechanics"]["sensor_polarity"]
        )
    finally:
        client.close()
        worker.join(1)


@pytest.mark.parametrize("failure", ["timeout", "unwritable_audit"])
def test_failed_edit_closes_channel_and_records_unknown_when_audit_is_writable(failure):
    hardware, _ = recorded_snapshot()
    parent, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    client = SnapshotClient(parent, hardware)
    entries = []

    def journal(entry):
        if failure == "unwritable_audit":
            raise OSError("injected persistent audit failure")
        entries.append(entry)

    command = {"id": 1, "expected_revision": 0, "bus": 0, "addr": 32, "patch": {"flap_offset": 5}}
    try:
        with pytest.raises(Unavailable):
            client.dispatch(command, journal)
        assert client.closed
        with pytest.raises(Unavailable):
            client.snapshot()
        if failure == "timeout":
            assert [entry["phase"] for entry in entries] == ["intent", "unknown"]
            assert entries[-1] == {
                "phase": "unknown",
                "command": command,
                "stage": "model_exchange",
                "error": "TimeoutError",
                "channel": "closed",
            }
        else:
            assert entries == []
    finally:
        client.close()
        peer.close()
