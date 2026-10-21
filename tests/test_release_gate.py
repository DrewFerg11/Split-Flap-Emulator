"""Release consumption rejects incomplete proof and never applies golden exceptions."""

import argparse
import copy
import json
import subprocess

import pytest

from tools.verify_release_gate import (
    LAYOUTS,
    WRONG_VERSION,
    GateFailure,
    assert_probe,
    inspect_result,
    read_factory,
    strict_command,
    validate_inputs,
    verify,
)

IMAGE = "ghcr.io/drewferg11/flapsim@sha256:" + "a" * 64
CHECKSUM = "b" * 64


def evidence(out, *, code=0, probe="none", checksum=CHECKSUM):
    run = out / "run"
    run.mkdir(parents=True)
    manifest = {
        "firmware": {"sha256": checksum},
        "passed": code == 0,
        "status": "passed" if code == 0 else "failed",
        "exit_code": code,
        "source_unchanged": True,
        "attempts": [{"returncode": 0}],
        "qemu_returncode": 0,
        "scenario": {"name": "release-smoke-8"},
        "wall_seconds": 2,
        "steps": [{"status": "passed"}],
    }
    if probe == "wrong-version":
        manifest.update(
            error="serial mismatch",
            steps=[
                {
                    "action": "expect_serial",
                    "status": "failed",
                    "expected": f"Firmware: {WRONG_VERSION} (",
                    "actual": "Firmware: v1.5.0 (abc)",
                }
            ],
        )
    if probe == "timeout":
        manifest.update(
            error="run exceeded its wall-clock deadline",
            scenario={"version": 1, "mode": "boot", "timeout": 240},
            timeout_seconds=3.0,
            steps=[],
        )
    (run / "run.json").write_text(json.dumps(manifest))
    (run / "junit.xml").write_text(f'<testsuite failures="{int(code != 0)}" skipped="0"/>')
    for name in ("serial.log", "timeline.jsonl", "report.md", "replay.html"):
        (run / name).touch()
    if probe == "wrong-version":
        (run / "serial.log").write_text("Firmware: v1.5.0 (prebuilt (GitHub Actions))\n")
    return manifest


@pytest.mark.parametrize("layout", LAYOUTS)
def test_strict_release_commands(layout):
    command = strict_command(layout, "v1.5.0-rc.1", "none")
    assert "--known-issues" not in command
    assert "FIRMWARE_VERSION=v1.5.0-rc.1" in command
    assert LAYOUTS[layout][0] in command and LAYOUTS[layout][1] in command
    assert "300" in command and command[command.index("--timing") + 1] == "ci"


@pytest.mark.parametrize("image", ["", "flapsim:latest", IMAGE.upper(), IMAGE + ":tag"])
def test_only_immutable_expected_registry_is_accepted(image):
    with pytest.raises(GateFailure, match="digest"):
        validate_inputs(image, "v1.5.0")


def test_strict_success_and_fail_closed_evidence(tmp_path):
    good = evidence(tmp_path)
    assert inspect_result(tmp_path, 0, CHECKSUM, "8", "none")["passed"]
    for key, value in [
        ("known_issues", {"applied": False}),
        ("version_exception", {}),
        ("status", "passed_with_known_issues"),
        ("source_unchanged", False),
        ("attempts", [{"returncode": None}]),
        ("qemu_returncode", 1),
        ("steps", [{"status": "xfail"}]),
        ("firmware", {"sha256": "wrong"}),
        ("scenario", {"name": "version-check"}),
    ]:
        altered = copy.deepcopy(good)
        altered[key] = value
        (tmp_path / "run/run.json").write_text(json.dumps(altered))
        with pytest.raises(GateFailure):
            inspect_result(tmp_path, 0, CHECKSUM, "8", "none")


@pytest.mark.parametrize("probe,code", [("wrong-version", 5), ("timeout", 4)])
def test_negative_run_must_fail_for_the_intended_reason(tmp_path, probe, code):
    manifest = evidence(tmp_path, code=code, probe=probe)
    with pytest.raises(GateFailure) as caught:
        inspect_result(tmp_path, code, CHECKSUM, "8", probe)
    assert caught.value.kind == probe
    manifest["error"] = "unrelated failure"
    manifest["steps"] = [{"status": "failed", "action": "wait_display"}]
    (tmp_path / "run/run.json").write_text(json.dumps(manifest))
    with pytest.raises(GateFailure) as caught:
        inspect_result(tmp_path, code, CHECKSUM, "8", probe)
    assert caught.value.kind == "simulation"


@pytest.mark.parametrize("native_code", [None, "0", True, -9])
def test_timeout_probe_rejects_missing_or_contradictory_child_exit(tmp_path, native_code):
    manifest = evidence(tmp_path, code=4, probe="timeout")
    manifest["qemu_returncode"] = native_code  # Last attempt remains a reaped exit 0.
    (tmp_path / "run/run.json").write_text(json.dumps(manifest))
    with pytest.raises(GateFailure) as caught:
        inspect_result(tmp_path, 4, CHECKSUM, "8", "timeout")
    assert caught.value.kind == "evidence"


@pytest.mark.parametrize(
    "key,value",
    [
        ("scenario", {"name": "release-smoke-8"}),
        ("timeout_seconds", 300),
        ("passed", True),
        ("status", "passed"),
        ("steps", [{"status": "failed", "action": "wait_display"}]),
    ],
)
def test_timeout_probe_rejects_a_different_or_inconsistent_run(tmp_path, key, value):
    manifest = evidence(tmp_path, code=4, probe="timeout")
    manifest[key] = value
    (tmp_path / "run/run.json").write_text(json.dumps(manifest))
    with pytest.raises(GateFailure) as caught:
        inspect_result(tmp_path, 4, CHECKSUM, "8", "timeout")
    assert caught.value.kind != "timeout"


def test_timeout_probe_requires_failed_junit(tmp_path):
    evidence(tmp_path, code=4, probe="timeout")
    (tmp_path / "run/junit.xml").write_text('<testsuite failures="0" skipped="0"/>')
    with pytest.raises(GateFailure) as caught:
        inspect_result(tmp_path, 4, CHECKSUM, "8", "timeout")
    assert caught.value.kind != "timeout"


def test_timeout_probe_can_prove_a_reaped_forced_shutdown(tmp_path):
    manifest = evidence(tmp_path, code=4, probe="timeout")
    manifest.update(qemu_returncode=-9, attempts=[{"returncode": -9}])
    (tmp_path / "run/run.json").write_text(json.dumps(manifest))
    with pytest.raises(GateFailure) as caught:
        inspect_result(tmp_path, 4, CHECKSUM, "8", "timeout")
    assert caught.value.kind == "timeout"  # An intentional deadline is still a failed gate.


def arguments(tmp_path, probe="none"):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    source = artifacts / "splitflap-esp32_n4-v1.5.0-factory.bin"
    factory = bytearray(b"\xff" * 65537)
    factory[0x1000] = factory[0x10000] = 0xE9
    source.write_bytes(factory)
    return argparse.Namespace(
        image=IMAGE,
        version="v1.5.0",
        layout="8",
        probe=probe,
        artifact_dir=artifacts,
        out=tmp_path / "output",
    )


def test_factory_headers_use_flash_offsets_and_reject_app_only(tmp_path):
    args = arguments(tmp_path)
    source = next(args.artifact_dir.iterdir())
    assert len(read_factory(source)) == 64
    source.write_bytes(b"\xe9" + b"\xff" * 65536)
    with pytest.raises(GateFailure, match="headers"):
        read_factory(source)


@pytest.mark.parametrize("probe", ["missing", "corrupt"])
def test_bad_artifact_blocks_before_docker_and_retains_proof(tmp_path, monkeypatch, probe):
    args = arguments(tmp_path, probe)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("Docker must not launch"))
    assert verify(args) == 1
    result = json.loads((args.out / "release-gate.json").read_text())
    assert result["error_kind"] == probe and result["firmware_sha256"]
    assert args.out.joinpath("summary.md").is_file()


def test_container_lifecycle_and_source_proof(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    calls = []

    def docker(command, **options):
        calls.append(command)
        if command[1] == "start":
            checksum = __import__("hashlib").sha256(next(args.artifact_dir.iterdir()).read_bytes())
            evidence(args.out, checksum=checksum.hexdigest())
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", docker)
    assert verify(args) == 0
    assert [c[1] for c in calls] == ["pull", "create", "start", "rm"]
    create = calls[1]
    assert create[create.index("--network") + 1] == "none"
    assert any("readonly" in item and "firmware.bin" in item for item in create)
    assert IMAGE in create and "--known-issues" not in create


@pytest.mark.parametrize("fault", ["cleanup", "integrity", "create-timeout"])
def test_failed_cleanup_or_changed_source_cannot_pass(tmp_path, monkeypatch, fault):
    args = arguments(tmp_path)
    calls = []

    def docker(command, **options):
        calls.append(command)
        if command[1] == "create" and fault == "create-timeout":
            raise subprocess.TimeoutExpired(command, options["timeout"])
        if command[1] == "start":
            source = next(args.artifact_dir.iterdir())
            checksum = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
            evidence(args.out, checksum=checksum)
            if fault == "integrity":
                source.unlink()
        return subprocess.CompletedProcess(command, int(command[1] == "rm" and fault == "cleanup"))

    monkeypatch.setattr(subprocess, "run", docker)
    assert verify(args) == 1
    assert calls[-1][1:3] == ["rm", "--force"]
    result = json.loads((args.out / "release-gate.json").read_text())
    assert result["error_kind"] == (
        "infrastructure-timeout" if fault == "create-timeout" else fault
    )


def test_skipped_junit_or_missing_report_invalidates_success(tmp_path):
    evidence(tmp_path)
    (tmp_path / "run/junit.xml").write_text('<testsuite failures="0" skipped="1"/>')
    with pytest.raises(GateFailure):
        inspect_result(tmp_path, 0, CHECKSUM, "8", "none")
    (tmp_path / "run/report.md").unlink()
    with pytest.raises(GateFailure, match="missing simulator artifact"):
        inspect_result(tmp_path, 0, CHECKSUM, "8", "none")


def test_docker_timeout_is_not_a_successful_timeout_probe(tmp_path, monkeypatch):
    args = arguments(tmp_path, "timeout")
    calls = []

    def docker(command, **options):
        calls.append(command)
        if command[1] == "start":
            raise subprocess.TimeoutExpired(command, options["timeout"])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", docker)
    assert verify(args) == 1
    assert calls[-1][1:3] == ["rm", "--force"]
    assert json.loads((args.out / "release-gate.json").read_text())["error_kind"] == (
        "infrastructure-timeout"
    )


def test_probe_assertion_requires_both_layouts_and_failed_publication(tmp_path):
    for layout in LAYOUTS:
        directory = tmp_path / f"release-sim-{layout}" / "evidence"
        directory.mkdir(parents=True)
        (directory / "release-gate.json").write_text(
            json.dumps(
                {
                    "passed": False,
                    "layout": layout,
                    "probe": "wrong-version",
                    "error_kind": "wrong-version",
                    "firmware_sha256": CHECKSUM,
                }
            )
        )
    assert_probe(tmp_path, "wrong-version", "failure", "failure")
    with pytest.raises(ValueError):
        assert_probe(tmp_path, "wrong-version", "failure", "success")
    (tmp_path / "release-sim-16/evidence/release-gate.json").unlink()
    with pytest.raises(OSError):
        assert_probe(tmp_path, "wrong-version", "failure", "failure")


@pytest.mark.parametrize(
    "serial",
    [
        "",
        "Firmware: (prebuilt)\n",
        "Firmware: garbage (prebuilt)\n",
        f"Firmware: {WRONG_VERSION} (prebuilt)\n",
        f"Firmware: v1.5.0 (prebuilt)\nFirmware: {WRONG_VERSION} (prebuilt)\n",
        "  Firmware: v1.5.0 (prebuilt)\n",
    ],
)
def test_wrong_version_probe_requires_a_distinct_reported_version(tmp_path, serial):
    evidence(tmp_path, code=5, probe="wrong-version")
    (tmp_path / "run/serial.log").write_text(serial)
    with pytest.raises(GateFailure) as caught:
        inspect_result(tmp_path, 5, CHECKSUM, "8", "wrong-version")
    assert caught.value.kind == "simulation"


def test_wrong_version_probe_reads_the_banner_beyond_the_step_tail(tmp_path):
    manifest = evidence(tmp_path, code=5, probe="wrong-version")
    manifest["steps"][0]["actual"] = "[ 38033][E][Preferences.cpp:50] begin(): ...\nHoming\n"
    (tmp_path / "run/run.json").write_text(json.dumps(manifest))
    (tmp_path / "run/serial.log").write_text(
        "Firmware: v1.5.0 (prebuilt (GitHub Actions))\n" + "noise\n" * 5000
    )
    with pytest.raises(GateFailure) as caught:
        inspect_result(tmp_path, 5, CHECKSUM, "8", "wrong-version")
    assert caught.value.kind == "wrong-version"
