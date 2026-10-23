"""Fail-closed checks of installed control/telemetry artifacts after owned cleanup."""

import collections
import json

from flapsim.state import validate_state
from flapsim.telemetry import MAX_BYTES, MAX_RECORDS, History


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
    ):
        raise ValueError("unexpected telemetry journal retention policy")
    history = History(control["run_id"])
    through = history.parse_cursor(journal.get("through"))
    previous = 0
    counts = collections.Counter()
    last = None
    for name in journal["files"]:
        path = out / name
        if not path.exists() and name == "i2c.previous.jsonl":
            continue
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise ValueError("missing or oversized telemetry journal")
        with path.open("rb") as log:
            for line in log:
                if not line.endswith(b"\n"):
                    raise ValueError("partial final telemetry record after shutdown")
                row = json.loads(line)
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
    if require_i2c and not counts["i2c"]:
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
