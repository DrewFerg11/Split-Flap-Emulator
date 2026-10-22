"""Power command safety: audit, idempotency, frozen views and failed transitions."""

import copy
import json
import socket
import threading

import pytest

from flapsim.control import ControlServer, SnapshotClient, Unavailable
from flapsim.lifecycle import ResetController
from flapsim.power import PowerController, QemuPower
from tests.test_control import recorded_snapshot
from tests.test_lifecycle import api, peer_loop


class Backend:
    fidelity = "test-checkpoint"

    def __init__(self, state, failure=None):
        self.state, self.failure, self.calls = state, failure, []

    def off(self, client):
        self.calls.append("off")
        if self.failure:
            raise OSError("lost checkpoint acknowledgement")
        return client._validate_snapshot(client._exchange(b"FLAPSIM_STATE/1"))

    def on(self, client, frozen):
        self.calls.append("on")
        self.state["guest_resets"] += 1
        return client._validate_snapshot(client._exchange(b"FLAPSIM_STATE/1"))


@pytest.fixture
def power_client():
    hardware, state = recorded_snapshot()
    parent, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    worker = threading.Thread(target=peer_loop, args=(peer, state))
    worker.start()
    client = SnapshotClient(parent, hardware)
    yield client, state
    client.close()
    worker.join(2)
    peer.close()
    assert not worker.is_alive()


@pytest.mark.parametrize("failure", [None, "intent", "result", "transport"])
def test_power_audit_unknown_outcomes_and_retry_never_repeat_transition(power_client, failure):
    client, state = power_client
    backend = Backend(state, failure == "transport")
    power = PowerController(client, backend)
    entries = []

    def journal(entry):
        if entry["phase"] == failure:
            raise OSError("injected audit failure")
        entries.append(copy.deepcopy(entry))

    command = {"id": 1, "on": False}
    if failure:
        with pytest.raises(Unavailable):
            power.apply(command, journal)
        assert power.state == "unknown" and client.closed
        assert entries[-1]["phase"] == "unknown"
        assert power.receipt["status"] == "unknown"
    else:
        receipt = power.apply(command, journal)
        assert receipt["status"] == "applied" and receipt["power_state"] == "off"
        frozen, status = client.view()
        assert status == "off" and frozen == client._validate_snapshot(copy.deepcopy(state))
        assert client.dispatch({"id": 1}, journal) == {"error": "controller_off"}
        assert ResetController(client).apply({"id": 1}, journal) == {"error": "controller_off"}
        assert client.snapshot() == frozen
        assert [r["phase"] for r in entries] == ["intent", "result"]
    calls = backend.calls[:]
    assert power.apply(command, journal) == power.receipt
    assert power.apply({"id": 1, "on": True}, journal) == {"error": "stale_or_reused_id"}
    assert backend.calls == calls
    if failure:
        assert power.apply({"id": 2, "on": True}, journal) == {"error": "backend_unavailable"}
    else:
        # A distinct same-state command is audited, but does not re-dispatch off.
        power.apply({"id": 2, "on": False}, journal)
        assert backend.calls == calls
        on = power.apply({"id": 3, "on": True}, journal)
        assert on["boot_generation"] == 1 and power.state == "on"
        assert client.view()[1] == "on"
        assert power.apply({"id": 3, "on": True}, journal) == on
        assert backend.calls == ["off", "on"]


def test_power_http_strict_bool_auth_frozen_label_and_receipts(tmp_path):
    hardware, state = recorded_snapshot()
    parent, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    worker = threading.Thread(target=peer_loop, args=(peer, state))
    worker.start()
    server = ControlServer(0, tmp_path, hardware, backend="native")
    backend = Backend(state)
    try:
        server.attach(parent, power=backend)
        for body, status in [
            (b'{"id":1}', 422),
            (b'{"id":1,"on":0}', 422),
            (b'{"id":1,"on":"false"}', 422),
            (b'{"id":1,"on":false,"extra":0}', 422),
            (b'{"id":true,"on":false}', 422),
            (b'{"id":1,"on":false,"on":true}', 400),
        ]:
            assert api(server, "/api/emu/power", body)[0] == status
        assert api(server, "/api/emu/power", b'{"id":1,"on":false}', authorized=False)[0] == 401
        assert (
            api(server, "/api/emu/power", b'{"id":1,"on":false}', origin="https://foreign.test")[0]
            == 403
        )
        assert backend.calls == []
        code, off, cache = api(server, "/api/emu/power", b'{"id":1,"on":false}')
        assert code == 202 and cache == "no-store"
        value = api(server, "/api/emu/state")[1]
        assert value["live"] is False and value["power_state"] == "off"
        assert value["capabilities"]["power"] is True
        assert api(server, "/api/emu/power/operations/1")[1] == off
        assert api(server, "/api/emu/power/operations/2")[0] == 404
        assert api(server, "/api/emu/reset", b'{"id":1}')[0] == 409
        assert api(server, "/api/emu/power", b'{"id":2,"on":true}')[0] == 202
        assert api(server, "/api/emu/state")[1]["live"] is True
        assert server.token not in (tmp_path / "control.jsonl").read_text()
    finally:
        server.close()
        worker.join(2)
        peer.close()
    assert not worker.is_alive()


def test_power_busy_rejection_does_not_consume_id(power_client):
    client, state = power_client
    power = PowerController(client, Backend(state))
    with client.lock:
        assert power.apply({"id": 1, "on": False}, lambda _: pytest.fail("not admitted")) == {
            "error": "backend_busy"
        }
        assert power.receipt is None


@pytest.mark.parametrize("running", [True, False])
def test_qemu_stop_is_observed_before_off_and_no_powerdown_or_reset(
    monkeypatch, power_client, running
):
    client, _ = power_client
    commands = []

    def qmp(path, names):
        commands.extend(names)
        return [{}, {"running": running}]

    monkeypatch.setattr("flapsim.power.qmp_commands", qmp)
    backend = QemuPower("unused")
    if running:
        with pytest.raises(ValueError, match="did not stop"):
            backend.off(client)
    else:
        assert backend.off(client) == client.snapshot()
    assert commands == ["stop", "query-status"]


def test_failed_native_power_on_never_signals_an_already_reaped_pid(monkeypatch):
    from types import SimpleNamespace

    from flapsim.power import NativePower

    hardware, state = recorded_snapshot()
    parent, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    client = SnapshotClient(parent, hardware)
    process = SimpleNamespace(pid=4321, returncode=0, wait=lambda timeout: 0, poll=lambda: 0)
    cleaned = []
    monkeypatch.setattr("flapsim.power.stop_group", lambda p: cleaned.append(p.pid) or 0)

    def launch(child, resume):
        raise OSError("injected launch failure")

    def checkpoint():
        with peer:
            assert peer.recv(4096) == b"FLAPSIM_POWER_OFF/1"
            peer.sendall(
                json.dumps({"state": state, "resume": f"2:1:{state['guest_ns']}:0"}).encode()
            )

    worker = threading.Thread(target=checkpoint)
    worker.start()
    owner = NativePower(process, launch)
    controller = PowerController(client, owner)
    try:
        controller.apply({"id": 1, "on": False}, lambda _: None)
        assert owner.poll() is None and owner.reaped
        with pytest.raises(Unavailable):
            controller.apply({"id": 2, "on": True}, lambda _: None)
        assert owner.stop() == 0
        assert cleaned == [4321]  # Only the immediate checkpoint cleanup, never the stale PID.
    finally:
        client.close()
        worker.join(2)
        peer.close()
