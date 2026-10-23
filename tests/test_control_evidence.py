"""Installed evidence gates reject incomplete captures and missing acceptance actions."""

import json

import pytest

from flapsim.hardware import load_hardware
from flapsim.telemetry import Collector
from tools.control_evidence import verify_action_results, verify_control_artifacts


@pytest.fixture
def evidence(tmp_path):
    hardware = load_hardware("esp32-8-single")
    collector = Collector(tmp_path, "run", hardware)
    collector.history.publish({"type": "i2c", "events": []})
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
