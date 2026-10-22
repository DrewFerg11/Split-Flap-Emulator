"""Reset idempotency, audit failures, generation observation and HTTP isolation."""

import copy
import http.client
import json
import socket
import threading

import pytest

from flapsim.control import ControlServer, SnapshotClient, Unavailable
from flapsim.lifecycle import ResetController, qmp_reset
from tests.test_control import recorded_snapshot


def peer_loop(peer, state, *, apply=True, extra=False, lost_ack=False):
    """A private peer, including a moving snapshot and controller generation changes."""
    with peer:
        try:
            while packet := peer.recv(4096):
                if packet == b"FLAPSIM_RESET/1":
                    if apply:
                        state["guest_resets"] += 2 if extra else 1
                    if lost_ack:
                        return
                    peer.sendall(b'{"status":"accepted"}')
                else:
                    assert packet == b"FLAPSIM_STATE/1"
                    peer.sendall(json.dumps(state).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.mark.parametrize(
    "failure", [None, "intent", "result", "extra_reset", "lost_ack", "unobserved"]
)
def test_reset_receipt_never_repeats_dispatch_and_audit_failure_is_unknown(failure):
    hardware, state = recorded_snapshot()
    parent, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    client = SnapshotClient(parent, hardware)
    controller = ResetController(client)
    entries = []

    def journal(entry):
        if entry["phase"] == failure:
            raise OSError("injected audit failure")
        entries.append(copy.deepcopy(entry))

    worker = threading.Thread(
        target=peer_loop,
        args=(peer, state),
        kwargs={
            "extra": failure == "extra_reset",
            "apply": failure != "unobserved",
            "lost_ack": failure == "lost_ack",
        },
    )
    worker.start()
    try:
        if failure:
            with pytest.raises(Unavailable):
                controller.apply({"id": 1}, journal)
            assert controller.receipt == {"command": {"id": 1}, "status": "unknown"}
            assert client.closed
        else:
            result = controller.apply({"id": 1}, journal)
            assert result["status"] == "applied"
            assert (result["before_generation"], result["boot_generation"]) == (0, 1)
            assert result["fidelity"] == "native-software-reexec"
        before_retry = state["guest_resets"]
        assert controller.apply({"id": 1}, journal) == controller.receipt
        assert state["guest_resets"] == before_retry
        assert controller.apply({"id": 0}, journal) == {"error": "stale_or_reused_id"}
        if failure:
            assert entries[-1]["phase"] == "unknown"
            if failure == "lost_ack":
                assert state["guest_resets"] == 1
                assert entries[-1]["stage"] == "reset_dispatch"
        else:
            assert [r["phase"] for r in entries] == ["intent", "result"]
            assert controller.apply({"id": 2}, journal)["boot_generation"] == 2
    finally:
        client.close()
        if worker:
            worker.join(2)
            assert not worker.is_alive()
        peer.close()


def test_reset_serializes_with_other_model_operations():
    hardware, _ = recorded_snapshot()
    parent, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    client = SnapshotClient(parent, hardware)
    with client.lock:
        controller = ResetController(client)
        assert controller.apply(
            {"id": 1}, lambda _: pytest.fail("must not audit unadmitted request")
        ) == {"error": "backend_busy"}
        assert controller.receipt is None
    client.close()
    peer.close()


def api(server, path, body=None, *, authorized=True, origin=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=7)
    headers = {"Content-Type": "application/json"}
    if authorized:
        headers["Authorization"] = "Bearer " + server.token
    if origin:
        headers["Origin"] = origin
    try:
        connection.request("GET" if body is None else "POST", path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read()), response.getheader("cache-control")
    finally:
        connection.close()


def test_reset_http_validation_auth_receipt_and_preserved_model(tmp_path):
    hardware, state = recorded_snapshot()
    original = copy.deepcopy(state)
    parent, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    server = ControlServer(0, tmp_path, hardware, backend="native")
    worker = threading.Thread(target=peer_loop, args=(peer, state))
    worker.start()
    try:
        server.attach(parent)
        assert server.metadata()["capabilities"]["reset"]
        for body, expected in [
            (b"{", 400),
            (b'{"id":1,"id":2}', 400),
            (b'{"id":true}', 422),
            (b'{"id":0}', 422),
            (b'{"id":1,"extra":0}', 422),
            (b"{}", 422),
            (b" " * 2049, 413),
        ]:
            assert api(server, "/api/emu/reset", body)[0] == expected
        assert api(server, "/api/emu/reset", b'{"id":1}', authorized=False)[0] == 401
        assert (
            api(server, "/api/emu/reset", b'{"id":1}', origin="http://untrusted.example")[0] == 403
        )
        assert state["guest_resets"] == 0
        code, applied, cache = api(server, "/api/emu/reset", b'{"id":1}')
        assert code == 202 and cache == "no-store"
        assert applied["status"] == "applied" and applied["boot_generation"] == 1
        assert api(server, "/api/emu/reset", b'{"id":1}')[1] == applied
        assert api(server, "/api/emu/reset/operations/1")[1] == applied
        assert api(server, "/api/emu/reset/operations/2")[0] == 404
        assert {k: v for k, v in state.items() if k != "guest_resets"} == {
            k: v for k, v in original.items() if k != "guest_resets"
        }
        assert state["guest_resets"] == 1
        assert server.token not in (tmp_path / "control.jsonl").read_text()
    finally:
        server.close()
        worker.join(2)
        assert not worker.is_alive()
        peer.close()


@pytest.mark.parametrize("failure", [None, "rejected", "eof", "oversized"])
def test_qmp_reset_matches_responses_and_does_not_retry(tmp_path, failure):
    path = tmp_path / "qmp.sock"
    commands = []
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen()

    def serve():
        with listener, listener.accept()[0] as connection:
            with connection.makefile("rb") as stream:
                for _ in range(2):
                    command = json.loads(stream.readline())
                    commands.append(command["execute"])
                    if failure == "eof":
                        return
                    if failure == "oversized":
                        connection.sendall(b"x" * 65537)
                        return
                    response = {
                        "id": command["id"],
                        "error" if failure == "rejected" else "return": {},
                    }
                    connection.sendall(
                        b'{"event":"RESET"}\n' + json.dumps(response).encode() + b"\n"
                    )
                    if failure:
                        return

    worker = threading.Thread(target=serve)
    worker.start()
    try:
        if failure:
            with pytest.raises((OSError, ValueError)):
                qmp_reset(path)
        else:
            qmp_reset(path)
            assert commands == ["qmp_capabilities", "system_reset"]
    finally:
        worker.join(2)
        assert not worker.is_alive()


def test_qmp_reset_controller_observes_generation_before_acknowledging():
    hardware, state = recorded_snapshot()
    parent, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    worker = threading.Thread(target=peer_loop, args=(peer, state))
    worker.start()
    client = SnapshotClient(parent, hardware)
    called = []

    def trigger():
        called.append(True)
        state["guest_resets"] += 1

    try:
        controller = ResetController(client, trigger)
        receipt = controller.apply({"id": 1}, lambda _: None)
        assert receipt["fidelity"] == "qmp-hard-reset"
        assert receipt["boot_generation"] == 1
        assert controller.apply({"id": 1}, lambda _: None) == receipt
        assert len(called) == 1
    finally:
        client.close()
        worker.join(2)
        peer.close()


def test_unknown_reset_remains_queryable_after_model_channel_closes(tmp_path):
    hardware, state = recorded_snapshot()
    parent, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    server = ControlServer(0, tmp_path, hardware, backend="native")
    worker = threading.Thread(target=peer_loop, args=(peer, state), kwargs={"lost_ack": True})
    worker.start()
    try:
        server.attach(parent)
        assert api(server, "/api/emu/reset", b'{"id":1}')[0] == 503
        code, receipt, _ = api(server, "/api/emu/reset/operations/1")
        assert code == 200 and receipt["status"] == "unknown"
        assert api(server, "/api/emu/reset", b'{"id":1}')[0] == 503
        assert api(server, "/api/emu/reset", b'{"id":2}')[0] == 503
        assert state["guest_resets"] == 1
    finally:
        server.close()
        worker.join(2)
        peer.close()
        assert not worker.is_alive()


def test_shutdown_during_admitted_reset_finishes_audit_and_reaps_listener(tmp_path):
    hardware, state = recorded_snapshot()
    parent, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    server = ControlServer(0, tmp_path, hardware, backend="qemu")
    dispatched, release = threading.Event(), threading.Event()
    results, failures = [], []
    peer_worker = threading.Thread(target=peer_loop, args=(peer, state))
    peer_worker.start()

    def trigger():
        dispatched.set()
        assert release.wait(2)
        state["guest_resets"] += 1

    def request_reset():
        try:
            results.append(api(server, "/api/emu/reset", b'{"id":1}'))
        except Exception as error:
            failures.append(error)

    server.attach(parent, reset=trigger)
    requester = threading.Thread(target=request_reset)
    requester.start()
    assert dispatched.wait(2)
    closer = threading.Thread(target=server.close)
    closer.start()
    try:
        # Closing detaches the server immediately, then waits for the admitted
        # request's client lock. New requests cannot start another operation.
        import time

        deadline = time.monotonic() + 2
        while server.client is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.client is None and closer.is_alive()
    finally:
        release.set()
        requester.join(4)
        closer.join(4)
        peer_worker.join(2)
        server.close()
        peer.close()
    assert not requester.is_alive() and not closer.is_alive() and not peer_worker.is_alive()
    assert not failures and results[0][0] == 202
    assert state["guest_resets"] == 1
    assert [
        json.loads(line)["phase"] for line in (tmp_path / "control.jsonl").read_text().splitlines()
    ] == ["intent", "result"]
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", server.port)) != 0
