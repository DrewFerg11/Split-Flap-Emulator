"""Installed evidence gates reject incomplete captures and missing acceptance actions."""

import json

import pytest

from flapsim.hardware import load_hardware
from flapsim.telemetry import EVENT, HEADER, MAGIC, Collector
from tools.control_evidence import verify_action_results, verify_control_artifacts


@pytest.fixture
def evidence(tmp_path):
    hardware = load_hardware("esp32-8-single")
    collector = Collector(tmp_path, "run", hardware)
    events = [(1, 10, 32, 1, 0), (2, 20, 32, (165 << 8) | 5, 0), (3, 30, 32, 3, 0)]
    collector.receive(
        HEADER.pack(MAGIC, 9, 1, 1, 0, 0, 32, 0, 1, 3, 30, 0, 0, 3)
        + b"".join(EVENT.pack(*event) for event in events)
    )
    collector.close()
    manifest = {
        "hardware": hardware,
        "control": {
            "run_id": "run",
            "status": "stopped",
            "capabilities": {"i2c_history": True, "stream": True},
            "telemetry": collector.metadata(),
        },
    }
    return tmp_path, manifest


def test_complete_evidence_has_terminal_cursor(evidence):
    out, manifest = evidence
    proof = verify_control_artifacts(out, manifest, require_i2c=True)
    assert proof["verified"] and proof["record_types"]["i2c"] == 1


@pytest.mark.parametrize(
    "corruption",
    ["partial", "foreign", "backwards", "terminal", "oversize", "flush", "running", "cleanup"],
)
def test_incomplete_evidence_never_verifies(evidence, corruption):
    out, manifest = evidence
    path = out / "i2c.jsonl"
    rows = path.read_text().splitlines()
    if corruption == "partial":
        path.write_text(path.read_text().rstrip("\n"))
    elif corruption == "foreign":
        row = json.loads(rows[0])
        row["run_id"] = "foreign"
        path.write_text(json.dumps(row) + "\n" + rows[1] + "\n")
    elif corruption == "backwards":
        path.write_text("\n".join(reversed(rows)) + "\n")
    elif corruption == "terminal":
        path.write_text(rows[0] + "\n")
    elif corruption == "oversize":
        with path.open("ab") as log:
            log.truncate(4 * 1024 * 1024 + 1)
    elif corruption == "flush":
        manifest["control"]["telemetry"]["journal"]["through"] = "run:3"
    elif corruption == "running":
        manifest["control"]["telemetry"]["status"] = "running"
    else:
        manifest["cleanup_error"] = "owned thread survived"
    with pytest.raises(ValueError):
        verify_control_artifacts(out, manifest)


def test_expected_failure_cannot_stand_in_for_executed_controls():
    steps = [
        {
            "action": action,
            "status": "passed",
            "actual": {
                "operations": [{"status": "applied"}] * (2 if action == "power_cycle" else 1)
            },
        }
        for action in ["set_mechanics", "set_fault", "reset", "power_cycle"]
    ]
    manifest = {"result": {"steps": steps}}
    verify_action_results(manifest)
    steps[1]["status"] = "xfail"
    with pytest.raises(ValueError, match="every action"):
        verify_action_results(manifest)
    steps[1]["status"] = "passed"
    steps[-1]["actual"]["operations"] = [{"status": "unknown"}]
    with pytest.raises(ValueError, match="applied receipts"):
        verify_action_results(manifest)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "bad"),
        ("producer_sequence", "0"),
        ("boot_generation", "18446744073709551616"),
        ("guest_ns", "9223372036854775808"),
        ("raw_sequence", "2"),
        ("transaction", "00"),
        ("bus", True),
        ("bus", -1),
        ("addr", 256),
        ("end", "unknown"),
        ("end", "fragment"),
        ("end", "orphan"),
        ("events", [{}] * 9),
        ("events", []),
        ("events", {}),
        ("events.0.sequence", "0"),
        ("events.1.sequence", "1"),
        ("events.0.guest_ns", "31"),
        ("events.1.guest_ns", "9"),
        ("events.0.addr", 256),
        ("events.0.event", 65536),
        ("events.0.event", True),
        ("events.0.kind", 5),
        ("events.0.data", 1),
        ("events.0.result", -1),
        ("events.0.ack", False),
        ("events.0.ack", 1),
        ("events.2.ack", True),
    ],
)
def test_corrupt_i2c_payload_never_counts_as_evidence(evidence, field, value):
    out, manifest = evidence
    path = out / "i2c.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    target = rows[0]
    parts = field.split(".")
    for key in parts[:-1]:
        target = target[int(key)] if isinstance(target, list) else target[key]
    target[parts[-1]] = value
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ValueError, match="I2C"):
        verify_control_artifacts(out, manifest, require_i2c=True)


@pytest.mark.parametrize(
    "field",
    [
        "source",
        "producer_sequence",
        "boot_generation",
        "guest_ns",
        "raw_sequence",
        "transaction",
        "bus",
        "addr",
        "end",
        "events",
    ],
)
def test_missing_i2c_fields_fail_closed(evidence, field):
    out, manifest = evidence
    path = out / "i2c.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    del rows[0][field]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ValueError, match="I2C"):
        verify_control_artifacts(out, manifest, require_i2c=True)


def test_empty_boundary_fragments_do_not_prove_i2c_activity(evidence):
    out, manifest = evidence
    path = out / "i2c.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0].update(end="reset", events=[])
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    verify_control_artifacts(out, manifest)
    with pytest.raises(ValueError, match="no structured I2C"):
        verify_control_artifacts(out, manifest, require_i2c=True)


@pytest.fixture
def rotated_evidence(evidence):
    out, manifest = evidence
    path = out / "i2c.jsonl"
    lines = path.read_text().splitlines(keepends=True)
    (out / "i2c.previous.jsonl").write_text(lines[0])
    path.write_text("".join(lines[1:]))
    manifest["control"]["telemetry"]["journal"]["rotations"] = 1
    return out, manifest


def test_complete_rotated_capture_verifies(rotated_evidence):
    out, manifest = rotated_evidence
    assert verify_control_artifacts(out, manifest, require_i2c=True)["verified"]


@pytest.mark.parametrize("name", ["i2c.previous.jsonl", "i2c.jsonl"])
@pytest.mark.parametrize("damage", ["missing", "empty", "partial"])
def test_partial_rotated_capture_fails_closed(rotated_evidence, name, damage):
    out, manifest = rotated_evidence
    path = out / name
    if damage == "missing":
        path.unlink()
    elif damage == "empty":
        path.write_bytes(b"")
    else:
        path.write_text(path.read_text().rstrip("\n"))
    with pytest.raises(ValueError):
        verify_control_artifacts(out, manifest)


def test_terminal_record_in_previous_file_cannot_hide_empty_current(rotated_evidence):
    out, manifest = rotated_evidence
    previous, current = out / "i2c.previous.jsonl", out / "i2c.jsonl"
    previous.write_text(previous.read_text() + current.read_text())
    current.write_bytes(b"")
    with pytest.raises(ValueError, match="empty"):
        verify_control_artifacts(out, manifest)


@pytest.mark.parametrize(
    "field", ["sequence", "guest_ns", "addr", "event", "kind", "data", "result", "ack"]
)
def test_truncated_event_never_verifies(evidence, field):
    out, manifest = evidence
    path = out / "i2c.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    del rows[0]["events"][0][field]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ValueError, match="I2C"):
        verify_control_artifacts(out, manifest, require_i2c=True)


def test_i2c_envelope_without_payload_never_verifies(evidence):
    out, manifest = evidence
    path = out / "i2c.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0] = {k: rows[0][k] for k in ["type", "run_id", "schema_version", "cursor"]}
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ValueError, match="I2C"):
        verify_control_artifacts(out, manifest, require_i2c=True)


@pytest.mark.parametrize("rotations", [-1, True, 1.0, "1", None])
def test_rotation_count_must_be_a_nonnegative_integer(evidence, rotations):
    out, manifest = evidence
    manifest["control"]["telemetry"]["journal"]["rotations"] = rotations
    with pytest.raises(ValueError, match="retention policy"):
        verify_control_artifacts(out, manifest)


def test_unexpected_previous_file_requires_rotation_metadata(evidence):
    out, manifest = evidence
    (out / "i2c.previous.jsonl").write_bytes(b"{}\n")
    with pytest.raises(ValueError, match="unexpected rotated"):
        verify_control_artifacts(out, manifest)


@pytest.mark.parametrize("end", ["fragment", "orphan"])
def test_valid_wire_fragment_shapes_remain_accepted(evidence, end):
    out, manifest = evidence
    path = out / "i2c.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    row = rows[0]
    row["end"] = end
    if end == "orphan":
        row.update(bus=2, transaction=None, events=row["events"][:1])
    else:
        event = row["events"][1]
        row["events"] = [{**event, "sequence": str(n)} for n in range(1, 9)]
        row["raw_sequence"] = "8"
    path.write_text("".join(json.dumps(item) + "\n" for item in rows))
    assert verify_control_artifacts(out, manifest, require_i2c=True)["verified"]
