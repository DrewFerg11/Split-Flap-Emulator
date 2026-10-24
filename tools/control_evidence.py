"""Fail-closed checks of installed control/telemetry artifacts after owned cleanup."""

import collections
import json
import re

from flapsim.state import validate_state
from flapsim.telemetry import ENDS, MAX_BYTES, MAX_RECORDS, History


def decimal(value, *, minimum=0, maximum=2**64 - 1):
    if (
        type(value) is not str
        or not 1 <= len(value) <= 20
        or not re.fullmatch(r"0|[1-9][0-9]*", value)
        or not minimum <= int(value) <= maximum
    ):
        raise ValueError("invalid I2C decimal field")
    return int(value)


def byte(value):
    return type(value) is int and 0 <= value <= 255


def verify_i2c_record(row):
    """Revalidate persisted wire evidence, independently of collection-time checks."""
    if not isinstance(row.get("source"), str) or not re.fullmatch(r"[0-9a-f]{16}", row["source"]):
        raise ValueError("invalid I2C source")
    decimal(row.get("producer_sequence"), minimum=1)
    decimal(row.get("boot_generation"))
    guest = decimal(row.get("guest_ns"), maximum=2**63 - 1)
    raw_sequence = decimal(row.get("raw_sequence"))
    if "transaction" not in row:
        raise ValueError("missing I2C transaction")
    if row["transaction"] is not None:
        decimal(row["transaction"], minimum=1)
    events = row.get("events")
    end = row.get("end")
    if (
        not byte(row.get("bus"))
        or not byte(row.get("addr"))
        or end not in ENDS.values()
        or not isinstance(events, list)
        or len(events) > 8
        or (end == "fragment" and len(events) != 8)
        or (end in {"stop", "orphan"} and not events)
        or (end == "orphan" and (len(events) != 1 or row["transaction"] is not None))
    ):
        raise ValueError("invalid I2C transaction fragment")
    previous = 0
    when = 0
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("invalid I2C event")
        sequence = decimal(event.get("sequence"), minimum=1)
        timestamp = decimal(event.get("guest_ns"), maximum=2**63 - 1)
        raw = event.get("event")
        if (
            not previous < sequence <= raw_sequence
            or not when <= timestamp <= guest
            or not byte(event.get("addr"))
            or type(raw) is not int
            or not 0 <= raw <= 65535
            or not byte(event.get("kind"))
            or event["kind"] != raw & 255
            or not byte(event.get("data"))
            or event["data"] != raw >> 8
            or not byte(event.get("result"))
        ):
            raise ValueError("invalid I2C event fields")
        ack = event["kind"] <= 2 or event["kind"] == 5
        if (
            ack and (type(event.get("ack")) is not bool or event["ack"] != (event["result"] == 0))
        ) or (not ack and "ack" in event):
            raise ValueError("invalid I2C acknowledgement")
        previous, when = sequence, timestamp
    if end == "stop" and events[-1]["kind"] != 3:
        raise ValueError("I2C stop fragment has no STOP event")
    return len(events)


def verify_control_artifacts(out, manifest, *, require_i2c=False, require_actions=False):
    control = manifest.get("control", {})
    telemetry = control.get("telemetry", {})
    if (
        control.get("status") != "stopped"
        or telemetry.get("status") != "stopped"
        or telemetry.get("error") is not None
        or telemetry.get("enabled") is not True
        or telemetry.get("format_version") != 1
        or telemetry.get("max_records") != MAX_RECORDS
        or telemetry.get("max_bytes") != MAX_BYTES
        or control.get("capabilities", {}).get("i2c_history") is not True
        or control.get("capabilities", {}).get("stream") is not True
        or manifest.get("cleanup_error")
    ):
        raise ValueError("missing successful control/telemetry cleanup evidence")
    journal = telemetry.get("journal", {})
    if (
        journal.get("files") != ["i2c.previous.jsonl", "i2c.jsonl"]
        or journal.get("bytes_per_file") != MAX_BYTES
        or type(journal.get("rotations")) is not int
        or journal["rotations"] < 0
    ):
        raise ValueError("unexpected telemetry journal retention policy")
    history = History(control["run_id"])
    through = history.parse_cursor(journal.get("through"))
    previous = 0
    counts = collections.Counter()
    last = None
    i2c_events = 0
    for name in journal["files"]:
        path = out / name
        if name == "i2c.previous.jsonl" and journal["rotations"] == 0:
            if path.exists():
                raise ValueError("unexpected rotated telemetry journal")
            continue
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
            raise ValueError("missing, empty or oversized telemetry journal")
        with path.open("rb") as log:
            for line in log:
                if not line.endswith(b"\n"):
                    raise ValueError("partial final telemetry record after shutdown")
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("telemetry journal record is not an object")
                if row.get("run_id") != control["run_id"] or row.get("schema_version") != 1:
                    raise ValueError("foreign telemetry journal record")
                kind = row.get("type")
                if kind not in {"i2c", "state", "lifecycle", "control", "gap"}:
                    raise ValueError("unknown telemetry record kind")
                if "cursor" in row:
                    sequence = history.parse_cursor(row["cursor"])
                    if sequence <= previous or sequence > through:
                        raise ValueError("telemetry journal cursor order mismatch")
                    previous = sequence
                elif kind != "gap" or row.get("reason") != "journal_retention":
                    raise ValueError("telemetry record has no cursor")
                if kind == "i2c":
                    i2c_events += verify_i2c_record(row)
                if kind == "state":
                    validate_state(row["state"], manifest["hardware"], require_settled=False)
                counts[kind] += 1
                last = row
    if (
        last is None
        or last.get("event") != "stream_stopped"
        or last.get("type") != "lifecycle"
        or previous != through
        or through == 0
    ):
        raise ValueError("telemetry journal did not flush its terminal cursor")
    if require_i2c and not i2c_events:
        raise ValueError("successful firmware run has no structured I2C evidence")
    if require_actions:
        verify_action_results(manifest)
        state = json.loads((out / "state.json").read_text())
        validate_state(state, manifest["hardware"])
        target = next(m for m in state["modules"] if m["bus"] == 1 and m["addr"] == 37)
        if (
            state.get("mechanics_revision") != 3
            or state["guest_resets"] < 3
            or target["mechanics"]["sensor_window"] != 42
            or target["faults"]["i2c"] != "ok"
            or target["fault_counters"]["nacks"] == 0
        ):
            raise ValueError("missing applied fault/repair/reset/power persistence evidence")
        phases = collections.Counter()
        with (out / "control.jsonl").open() as audit:
            for line in audit:
                phases[json.loads(line)["phase"]] += 1
        if phases["unknown"] or phases["intent"] < 7 or phases["result"] != phases["intent"]:
            raise ValueError("incomplete scenario control audit")
    return {"verified": True, "record_types": dict(counts), "journal_through": journal["through"]}


def verify_action_results(manifest):
    steps = manifest.get("result", {}).get("steps", [])
    controls = {"set_mechanics", "set_fault", "reset", "power_cycle"}
    if not controls <= {s["action"] for s in steps} or any(s["status"] != "passed" for s in steps):
        raise ValueError("runtime control acceptance did not pass every action")
    for step in steps:
        if step["action"] in controls:
            receipts = step.get("actual", {}).get("operations", [])
            if len(receipts) != (2 if step["action"] == "power_cycle" else 1) or any(
                receipt.get("status") != "applied" for receipt in receipts
            ):
                raise ValueError("runtime control acceptance is missing applied receipts")
