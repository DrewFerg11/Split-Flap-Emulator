"""Keep useful live evidence when strict firmware/model verification fails."""

import io
import json
from types import SimpleNamespace

import pytest

from flapsim.executor import BootExecutor, ExecutionFailed
from tools import verify_control


def test_failed_initial_settlement_keeps_motion_evidence_without_pausing(tmp_path, monkeypatch):
    (tmp_path / "run.json").write_text(
        json.dumps(
            {
                "control": {"endpoint": "http://127.0.0.1:1"},
                "command": ["-qmp", "unix:/unused,server=on"],
            }
        )
    )
    (tmp_path / "control-token").write_text("test-only\n")
    moving = {"live": True, "capabilities": {"reset": False}, "state": {"settled": False}}
    opener = SimpleNamespace(open=lambda *_args, **_kwargs: io.StringIO(json.dumps(moving)))
    monkeypatch.setattr(verify_control.urllib.request, "build_opener", lambda *_: opener)

    def failed_settlement(*_):
        raise ExecutionFailed("injected model stall")

    def unexpected_pause(*_):
        pytest.fail("controller commands must wait for a successful initial settlement")

    monkeypatch.setattr(BootExecutor, "execute", failed_settlement)
    monkeypatch.setattr(verify_control, "qmp", unexpected_pause)
    context = SimpleNamespace(out=tmp_path, hardware={"modules": [{}]}, checkpoint=lambda: None)
    with pytest.raises(ExecutionFailed, match="model stall"):
        verify_control.ControlProbe().execute(context)
    evidence = json.loads((tmp_path / "control-verification.json").read_text())
    assert evidence == {"complete": False, "moving": moving}
