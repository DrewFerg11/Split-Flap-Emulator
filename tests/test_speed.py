"""Rate changes use the model lock, durable audit, and a separate receipt namespace."""

import copy
import json
import socket
import threading
from types import SimpleNamespace

import pytest

from flapsim.control import ControlServer, SnapshotClient, Unavailable
from flapsim.speed import SpeedController
from tests.test_control import recorded_snapshot
from tests.test_lifecycle import api


def speed_peer(peer, state, calls, *, lost=False, wrong=False):
    with peer:
        try:
            speed = 5
            while packet := peer.recv(4096):
                if packet == b"FLAPSIM_STATE/1":
                    peer.sendall(json.dumps(state).encode())
                    continue
                assert packet.startswith(b"FLAPSIM_SPEED/1")
                if packet != b"FLAPSIM_SPEED/1":
                    command = json.loads(packet[len(b"FLAPSIM_SPEED/1 ") :])
                    calls.append(command)
                    speed = command["speed"]
                    if lost:
                        return
                peer.sendall(json.dumps({"speed": 1 if wrong else speed, "state": state}).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.mark.parametrize("failure", [None, "intent", "result", "lost", "wrong"])
def test_speed_unknown_outcomes_deduplication_and_serialization(failure):
    hardware, state = recorded_snapshot()
    original = copy.deepcopy(state)
    parent, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    calls, entries = [], []
    worker = threading.Thread(
        target=speed_peer,
        args=(peer, state, calls),
        kwargs={"lost": failure == "lost", "wrong": failure == "wrong"},
    )
    worker.start()
    client = SnapshotClient(parent, hardware)
    speed = SpeedController(client, 5)

    def journal(entry):
        if entry["phase"] == failure:
            raise OSError("audit unavailable")
        entries.append(copy.deepcopy(entry))

    command = {"id": 1, "speed": 0.25}
    try:
        with client.lock:
            assert speed.apply(command, journal) == {"error": "backend_busy"}
            assert speed.receipt is None
        client.power = SimpleNamespace(state="off")
        assert speed.apply(command, journal) == {"error": "controller_off"}
        assert speed.apply(None, journal)["current"] == 5
        assert not entries and not calls
        client.power = None
        if failure:
            with pytest.raises(Unavailable):
                speed.apply(command, journal)
            assert client.closed and speed.current is None
            assert entries[-1]["phase"] == "unknown"
            assert speed.receipt["status"] == "unknown"
        else:
            receipt = speed.apply(command, journal)
            assert receipt["speed"] == 0.25 and receipt["boot_generation"] == 0
            assert speed.apply(None, journal)["current"] == 0.25
            assert [entry["phase"] for entry in entries] == ["intent", "result"]
        dispatched = len(calls)
        assert speed.apply(command, journal) == speed.receipt
        assert len(calls) == dispatched
        assert speed.apply({"id": 1, "speed": 2}, journal) == {"error": "stale_or_reused_id"}
        assert state == original
        if failure:
            assert speed.apply({"id": 2, "speed": 2}, journal) == {"error": "backend_unavailable"}
    finally:
        client.close()
        worker.join(2)
        assert not worker.is_alive()


def test_speed_http_validation_auth_and_receipt(tmp_path):
    hardware, state = recorded_snapshot()
    calls = []
    parent, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    worker = threading.Thread(target=speed_peer, args=(peer, state, calls))
    worker.start()
    server = ControlServer(0, tmp_path, hardware, backend="native")
    try:
        server.attach(parent)
        for value in [True, "2", None, 0, 51, -1, 10**300]:
            assert api(server, "/api/emu/speed", json.dumps({"id": 1, "speed": value}))[0] == 422
        for body, status in [
            (b'{"id":1,"speed":NaN}', 400),
            (b'{"id":true,"speed":1}', 422),
            (b'{"id":1,"speed":2,"extra":0}', 422),
            (b'{"id":1,"speed":2,"speed":3}', 400),
            (b" " * 2049, 413),
        ]:
            assert api(server, "/api/emu/speed", body)[0] == status
        body = b'{"id":1,"speed":0.25}'
        assert api(server, "/api/emu/speed", body, authorized=False)[0] == 401
        assert api(server, "/api/emu/speed", body, origin="http://foreign.example")[0] == 403
        code, result, cache = api(server, "/api/emu/speed", body)
        assert code == 202 and cache == "no-store"
        assert api(server, "/api/emu/speed", body)[1] == result
        assert api(server, "/api/emu/speed/operations/1")[1] == result
        assert api(server, "/api/emu/speed/operations/2")[0] == 404
        assert api(server, "/api/emu/state")[1]["speed"]["current"] == 0.25
        assert calls == [{"id": 1, "speed": 0.25}]
    finally:
        server.close()
        worker.join(2)
        assert not worker.is_alive()


def test_qemu_reports_startup_timing_and_rejects_live_rate_without_dispatch(tmp_path):
    hardware, _ = recorded_snapshot()
    server = ControlServer(0, tmp_path, hardware, timing="realtime")
    try:
        status, result, _ = api(server, "/api/emu/speed")
        assert status == 200 and result["timing"] == "realtime" and not result["runtime"]
        status, result, _ = api(server, "/api/emu/speed", b'{"id":1,"speed":2}')
        assert status == 409 and result["error"] == "unsupported_capability"
        assert api(server, "/api/emu/speed/operations/1")[0] == 409
        assert not (tmp_path / "control.jsonl").exists()
    finally:
        server.close()
