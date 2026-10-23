"""History loss, wire fidelity, durable bounds and the real HTTP/WebSocket boundary."""

import json
import socket
import urllib.error
import urllib.request

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from flapsim.control import ControlServer, channel
from flapsim.hardware import load_hardware
from flapsim.telemetry import EVENT, HEADER, MAGIC, Collector, CursorError, History


def packet(
    sequence=1,
    *,
    kind=0,
    events=(),
    source=9,
    transaction=1,
    end=1,
    drops=0,
    dropped_events=0,
    raw_sequence=100,
):
    payload = b"".join(EVENT.pack(*event) for event in events)
    return (
        HEADER.pack(
            MAGIC,
            source,
            sequence,
            transaction,
            0,
            0,
            32,
            kind,
            end,
            len(events),
            100,
            drops,
            dropped_events,
            raw_sequence,
        )
        + payload
    )


def test_cursor_pages_earliest_after_and_explicit_retention():
    history = History("run", max_records=3)
    for n in range(6):
        history.publish({"type": "i2c", "n": n})
    page = history.page(limit=2)
    assert [r["n"] for r in page["records"]] == [3, 4]
    assert page["gap"] == {
        "reason": "retention",
        "after": "run:0",
        "through": "run:3",
        "records": "3",
    }
    assert page["more"] and page["next"] == "run:5"
    page = history.page(page["next"])
    assert [r["n"] for r in page["records"]] == [5]
    assert not page["more"] and page["gap"] is None
    assert history.page(page["next"])["records"] == []
    for cursor in ["run:01", "run:-1", "run:１", "run:18446744073709551616"]:
        with pytest.raises(CursorError, match="invalid_cursor"):
            history.page(cursor)
    for cursor, error in [("other:1", "foreign_run_cursor"), ("run:7", "future_cursor")]:
        with pytest.raises(CursorError, match=error) as caught:
            history.page(cursor)
        assert caught.value.status == 409
    history.sequence = 2**53
    history.records.clear()
    history.publish({"type": "lifecycle"})
    assert history.page(f"run:{2**53}")["next"] == f"run:{2**53 + 1}"


def test_byte_retention_and_empty_oversized_record_have_explicit_gap():
    history = History("run", max_bytes=200)
    history.publish({"large": "x" * 201})
    page = history.page()
    assert not page["records"] and page["gap"]["records"] == "1"
    assert page["next"] == "run:1" and history.bytes == 0


def test_wire_results_loss_and_unknown_tail_are_honest(tmp_path):
    collector = Collector(tmp_path, "run", load_hardware("esp32-8-single"))
    try:
        collector.receive(packet(events=[(1, 90, 32, 1, 1), (2, 95, 32, 6, 255)]))
        collector.receive(packet(3, events=[(3, 100, 33, 0xA505, 0)], drops=1, dropped_events=4))
        collector.receive(packet(1, source=10, kind=1))
        rows = collector.history.page()["records"]
        assert rows[0]["events"][0]["ack"] is False
        assert rows[0]["events"][1]["result"] == 255
        assert "ack" not in rows[0]["events"][1]
        assert rows[1]["reason"] == "producer_loss" and rows[1]["dropped_events"] == "4"
        assert rows[2]["events"][0]["data"] == 0xA5
        assert rows[2]["events"][0]["addr"] == 33
        assert rows[3]["reason"] == "source_tail_unknown" and rows[3]["exact_count"] is None
        assert collector.metadata()["producer_dropped_events"] == "4"
        with pytest.raises(ValueError, match="backwards"):
            collector.receive(packet(1, source=10))
    finally:
        collector.close()
    lines = [json.loads(line) for line in (tmp_path / "i2c.jsonl").read_text().splitlines()]
    assert lines[-1]["event"] == "stream_stopped"
    assert collector.metadata()["journal"]["through"] == collector.history.page()["latest"]


@pytest.mark.parametrize(
    "data", [b"", b"bad", packet()[:-1], packet(kind=8), packet(events=[(1, 0, 32, 1, 256)])]
)
def test_invalid_wire_is_rejected(tmp_path, data):
    collector = Collector(tmp_path, "run", load_hardware("esp32-8-single"))
    try:
        with pytest.raises(ValueError):
            collector.receive(data)
    finally:
        collector.close()


def test_rotating_evidence_is_bounded_and_journal_lag_is_explicit(tmp_path):
    collector = Collector(
        tmp_path, "run", load_hardware("esp32-8-single"), journal_bytes=1000, max_records=3
    )
    # Holding the history lock keeps the writer behind while collection continues.
    with collector.history.condition:
        for n in range(100):
            collector.history.publish({"type": "i2c", "n": n, "padding": "x" * 200})
    collector.close()
    rows = []
    for name in ["i2c.previous.jsonl", "i2c.jsonl"]:
        path = tmp_path / name
        if path.exists():
            assert path.stat().st_size <= 1000
            rows += [json.loads(line) for line in path.read_text().splitlines()]
    assert any(r.get("reason") == "journal_retention" for r in rows)
    assert rows[-1]["event"] == "stream_stopped"
    assert not collector.writer.is_alive()


def api(service, path, *, token=True):
    request = urllib.request.Request(
        service.endpoint + path,
        headers={"Authorization": "Bearer " + service.token} if token else {},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(request, timeout=3)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


def test_http_and_websocket_share_history_and_enforce_auth(tmp_path):
    service = ControlServer(0, tmp_path, load_hardware("esp32-8-single"), telemetry=True)
    url = service.endpoint.replace("http:", "ws:") + "/api/emu/stream"
    try:
        service.telemetry.receive(packet(events=[(1, 90, 32, 1, 0)]))
        assert api(service, "/api/emu/i2c", token=False)[0] == 401
        status, page = api(service, "/api/emu/i2c?limit=1")
        assert status == 200 and page["records"][0]["events"][0]["ack"]
        for query, expected in [
            ("limit=0", 422),
            ("limit=1&limit=2", 422),
            ("token=secret", 422),
            ("since=other:1", 409),
        ]:
            assert api(service, "/api/emu/i2c?" + query)[0] == expected
        with connect(url, proxy=None) as ws:
            ws.send(json.dumps({"token": service.token}))
            assert json.loads(ws.recv(timeout=2))["records"] == page["records"]
            service.telemetry.receive(packet(2, kind=2))
            assert json.loads(ws.recv(timeout=2))["records"][0]["event"] == "reset"
        with connect(
            url + "?since=" + page["next"],
            additional_headers={"Authorization": "Bearer " + service.token},
            proxy=None,
        ) as ws:
            assert json.loads(ws.recv(timeout=2))["records"][0]["event"] == "reset"
        with connect(url, proxy=None) as ws:
            ws.send(json.dumps({"token": "wrong"}))
            with pytest.raises(ConnectionClosed) as caught:
                ws.recv(timeout=2)
            assert caught.value.rcvd.code == 1008
        with pytest.raises(InvalidStatus):
            with connect(url, origin="http://evil.example", proxy=None):
                pass
        with connect(url, proxy=None) as ws:
            with pytest.raises(ConnectionClosed):
                ws.recv(timeout=2)  # no history before authentication or after auth timeout
        with connect(url, proxy=None) as ws:
            ws.send(b"binary-auth")
            with pytest.raises(ConnectionClosed):
                ws.recv(timeout=2)
    finally:
        service.close()
    assert service.subscribers == 0 and not service.telemetry.writer.is_alive()
    assert service.token not in (tmp_path / "i2c.jsonl").read_text()


def test_disabled_capabilities_and_transport_eof_cleanup(tmp_path):
    hardware = load_hardware("esp32-8-single")
    service = ControlServer(0, tmp_path, hardware)
    try:
        assert api(service, "/api/emu/i2c")[0] == 409
        assert not service.capabilities["stream"] and not service.capabilities["i2c_history"]
    finally:
        service.close()
    (tmp_path / "control-token").unlink()
    service = ControlServer(0, tmp_path, hardware, telemetry=True)
    try:
        with channel(service) as (env, fds, parent, child):
            assert len(fds) == 2
            writer = socket.socket(fileno=socket.dup(int(env["FLAPSIM_I2C_FD"])))
            writer.sendall(packet(kind=1))
            writer.close()
        rows = service.telemetry.history.page()["records"]
        assert rows[-1]["reason"] == "source_tail_unknown"
        assert not service.telemetry.reader.is_alive()
    finally:
        service.close()


def test_websocket_subscriber_bound_and_shutdown(tmp_path):
    from contextlib import ExitStack

    service = ControlServer(0, tmp_path, load_hardware("esp32-8-single"), telemetry=True)
    url = service.endpoint.replace("http:", "ws:") + "/api/emu/stream"
    try:
        with ExitStack() as stack:
            clients = []
            for _ in range(8):
                ws = stack.enter_context(
                    connect(
                        url,
                        additional_headers={"Authorization": "Bearer " + service.token},
                        proxy=None,
                    )
                )
                clients.append(ws)
                assert json.loads(ws.recv(timeout=2))["records"] == []
            assert service.subscribers == 8
            with pytest.raises(InvalidStatus):
                with connect(url, proxy=None):
                    pass
            service.telemetry.history.fail(OSError("disk unavailable"))
            assert api(service, "/api/emu/i2c")[0] == 503
            for ws in clients:
                assert json.loads(ws.recv(timeout=2))["error"]
                with pytest.raises(ConnectionClosed):
                    ws.recv(timeout=2)
    finally:
        with pytest.raises(RuntimeError, match="structured telemetry failed"):
            service.close()
    assert not service.thread.is_alive() and not service.telemetry.writer.is_alive()
