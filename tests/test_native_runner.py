"""`flapsim run --mode native` supervision, provenance, inputs and baselines (fake child)."""

import argparse
import json
import os
import socket
import sys
from pathlib import Path

import pytest

from flapsim import native
from flapsim.baseline import KnownIssues, load_known_issues
from flapsim.cli import check_mode_arguments
from flapsim.executor import load_executor
from flapsim.hardware import load_hardware
from flapsim.runner import run

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "062f5f4eab842326883dbb919587dbcd76f22862"


def fake_build(path, *, commit=COMMIT, dirty=False):
    (path / "bin").mkdir(parents=True)
    (path / "littlefs").mkdir()
    (path / "littlefs/timezones.json").write_text("{}")
    binary = path / "bin/flapsim-native"
    binary.write_text(
        f'#!/bin/sh\nexec {sys.executable} {ROOT / "tests/fixtures/fake_native.py"} "$@"\n'
    )
    binary.chmod(0o755)
    info = {
        "schema_version": 1,
        "firmware": {
            "source": "golden",
            "ref": "v1.4.1",
            "commit": commit,
            "dirty": dirty,
            "version": "v1.4.1",
        },
        "shim_sha256": "0" * 64,
    }
    (path / "build-info.json").write_text(json.dumps(info))
    return path


@pytest.fixture
def native_args(tmp_path):
    return argparse.Namespace(
        mode="native",
        native_build=fake_build(tmp_path / "build"),
        firmware_src=None,
        speed=None,
        hardware="esp32-8-single",
        scenario="boot",
        variables=[],
        known_issues=None,
        out=tmp_path / "out",
        seed=7,
        timeout=20,
        trace=False,
    )


def manifest(args):
    return json.loads((args.out / "run.json").read_text())


def assert_reaped(value):
    attempt = value["attempts"][0]
    assert isinstance(attempt["returncode"], int)
    with pytest.raises(ProcessLookupError):
        os.kill(attempt["pid"], 0)
    with socket.socket() as connection:
        assert connection.connect_ex(("127.0.0.1", attempt["port"])) != 0


def test_native_boot_records_mode_source_binary_and_reaps(native_args):
    assert run(native_args) == 0
    value = manifest(native_args)
    assert value["mode"] == "native" and value["status"] == "passed"
    assert value["firmware"]["commit"] == COMMIT and value["firmware"]["version"] == "v1.4.1"
    binary = native_args.native_build / "bin/flapsim-native"
    assert (
        value["firmware"]["binary"]["sha256"]
        == native.load_build(native_args.native_build)["binary_sha256"]
    )
    assert value["firmware"]["binary"]["path"] == str(binary.resolve())
    assert (
        value["native"]["speed"] == native.DEFAULT_SPEED
        and value["native"]["shim_sha256"] == "0" * 64
    )
    assert value["source_unchanged"] is True and value["native_returncode"] is not None
    assert "build" not in value and "timing" not in value
    config = json.loads((native_args.out / "native-config.json").read_text())
    assert config["speed"] == native.DEFAULT_SPEED and config["random_seed"] == 7
    assert (native_args.out / "littlefs/timezones.json").read_text() == "{}"
    assert_reaped(value)


def test_inherited_listener_serves_http_across_an_emulated_reset(
    native_args, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLAPSIM_FAKE_MODE", "reexec")
    scenario = tmp_path / "http.yaml"
    scenario.write_text(
        "version: 1\nname: http\ntimeout: 20\nmodes: [native]\nsteps:\n"
        "  - wait_serial: 'flapsim native boot 2'\n"
        "  - http: {path: /version, status: 200, json: {boot: 2}, wait_ready: true}\n"
    )
    native_args.scenario = str(scenario)
    native_args.speed = 50
    assert run(native_args) == 0, manifest(native_args).get("error")
    first = json.loads((native_args.out / "boot-1.json").read_text())
    second = json.loads((native_args.out / "boot-2.json").read_text())
    assert first["pid"] == second["pid"] == manifest(native_args)["attempts"][0]["pid"]
    assert second["config"]["speed"] == 50
    assert_reaped(manifest(native_args))


@pytest.mark.parametrize("mode,code,error", [("exit", 3, "exited early"), ("hang", 4, "deadline")])
def test_native_child_failures_are_bounded(native_args, monkeypatch, mode, code, error):
    monkeypatch.setenv("FLAPSIM_FAKE_MODE", mode)
    native_args.timeout = 2
    assert run(native_args) == code
    value = manifest(native_args)
    assert value["passed"] is False and error in value["error"]
    assert_reaped(value)


def test_native_executable_tampering_fails(native_args, monkeypatch):
    monkeypatch.setenv("FLAPSIM_FAKE_MODE", "tamper")
    monkeypatch.setenv("FLAPSIM_FAKE_SOURCE", str(native_args.native_build / "bin/flapsim-native"))
    assert run(native_args) == 3
    value = manifest(native_args)
    assert value["source_unchanged"] is False and "native executable changed" in value["error"]


@pytest.mark.parametrize(
    "mutation,message",
    [
        (lambda p: (p / "bin/flapsim-native").chmod(0o644), "missing native executable"),
        (lambda p: (p / "build-info.json").write_text("{"), "provenance"),
        (lambda p: (p / "build-info.json").write_text('{"schema_version": 2}'), "invalid native"),
        (lambda p: __import__("shutil").rmtree(p / "littlefs"), "LittleFS"),
    ],
)
def test_invalid_native_builds_fail_before_launch(native_args, mutation, message):
    mutation(native_args.native_build)
    with pytest.raises(ValueError, match=message):
        run(native_args)
    assert not native_args.out.exists()


def cli_args(**values):
    base = {
        "mode": "qemu",
        "firmware": None,
        "native_build": None,
        "firmware_src": None,
        "speed": None,
        "timing": None,
        "control_port": None,
    }
    return argparse.Namespace(**{**base, **values})


@pytest.mark.parametrize(
    "values,message",
    [
        ({}, "requires --firmware"),
        ({"firmware": Path("x.bin"), "speed": 5.0}, "--speed applies only"),
        ({"firmware": Path("x.bin"), "native_build": Path("b")}, "--native-build applies only"),
        ({"mode": "native"}, "exactly one"),
        ({"mode": "native", "native_build": Path("b"), "firmware_src": "golden"}, "exactly one"),
        (
            {"mode": "native", "native_build": Path("b"), "timing": "ci"},
            "apply only to --mode qemu",
        ),
    ],
)
def test_each_mode_accepts_only_its_own_inputs(values, message):
    with pytest.raises(ValueError, match=message):
        check_mode_arguments(cli_args(**values))


def test_qemu_mode_keeps_its_ci_timing_default():
    args = cli_args(firmware=Path("x.bin"))
    check_mode_arguments(args)
    assert args.timing == "ci"


@pytest.mark.parametrize("speed", [0.1, 51, True, "5"])
def test_speed_is_bounded(speed):
    with pytest.raises(ValueError, match="--speed"):
        native.check_speed(speed)


def test_native_baseline_binds_a_clean_source_commit():
    baseline = load_known_issues(ROOT / "baselines/v1.4.1-native.yaml")
    assert baseline.firmware_source_commit == COMMIT and baseline.firmware_sha256 is None
    baseline.check_source(COMMIT, False)
    with pytest.raises(ValueError, match="clean firmware source"):
        baseline.check_source(COMMIT, True)
    with pytest.raises(ValueError, match="clean firmware source"):
        baseline.check_source("1" * 40, False)
    with pytest.raises(ValueError, match="binds native firmware source"):
        baseline.check_firmware("0" * 64)
    golden = load_known_issues(ROOT / "baselines/v1.4.1-golden.yaml")
    with pytest.raises(ValueError, match="binds a QEMU firmware image"):
        golden.check_source(COMMIT, False)


def test_native_baseline_carries_the_golden_firmware_defect_entries():
    # The same v1.4.1 defects (SFD#86 splices, SFD#54 item 4) must be expected in both modes.
    native = load_known_issues(ROOT / "baselines/v1.4.1-native.yaml")
    golden = load_known_issues(ROOT / "baselines/v1.4.1-golden.yaml")

    def splices(baseline):
        return [e["regex"] for e in baseline.serial if e["issue"].endswith("#86")]

    assert len(splices(golden)) == 2 and splices(native) == splices(golden)
    key = ("release-smoke-16", 11, "assert_no_errors")
    shared = [
        [e["error"] for e in b.failures if (e["scenario"], e["step"], e["action"]) == key]
        for b in (native, golden)
    ]
    assert len(shared[0]) == 1 and shared[0] == shared[1]


def test_baseline_binding_is_one_validated_identity():
    entry = {"issue": "a/b#1", "reason": "r", "regex": "^x$"}
    with pytest.raises(ValueError, match="full lowercase commit"):
        KnownIssues(
            {"version": 1, "firmware_source_commit": "ABC", "description": "d", "serial": [entry]}
        )
    with pytest.raises(ValueError, match="expected fields"):
        KnownIssues(
            {
                "version": 1,
                "firmware_source_commit": COMMIT,
                "firmware_sha256": "0" * 64,
                "description": "d",
                "serial": [entry],
            }
        )


def test_native_baseline_rejects_a_dirty_build_before_launch(native_args, tmp_path):
    native_args.native_build = fake_build(tmp_path / "dirty", dirty=True)
    native_args.known_issues = ROOT / "baselines/v1.4.1-native.yaml"
    native_args.scenario = "release-smoke-8"
    native_args.variables = ["FIRMWARE_VERSION=v1.4.1"]
    with pytest.raises(ValueError, match="clean firmware source"):
        run(native_args)


def test_scenario_modes_are_validated_and_enforced(tmp_path):
    hardware = load_hardware("esp32-8-single")
    scenario = tmp_path / "s.yaml"
    body = "version: 1\nname: s\ntimeout: 5\n{modes}steps:\n  - sleep_guest: 1\n"
    scenario.write_text(body.format(modes="modes: [qemu]\n"))
    load_executor(str(scenario), hardware)
    with pytest.raises(ValueError, match="does not support native mode"):
        load_executor(str(scenario), hardware, mode="native")
    for invalid in [
        "modes: []\n",
        "modes: [qemu, qemu]\n",
        "modes: [hardware]\n",
        "modes: native\n",
    ]:
        scenario.write_text(body.format(modes=invalid))
        with pytest.raises(ValueError, match="scenario modes"):
            load_executor(str(scenario), hardware)
    scenario.write_text(body.format(modes=""))
    assert load_executor(str(scenario), hardware, mode="native")


def test_native_mode_accepts_control_port():
    check_mode_arguments(cli_args(mode="native", native_build=Path("b"), control_port=0))


@pytest.mark.parametrize("early_exit", [False, True])
def test_native_control_listener_cleanup(native_args, monkeypatch, early_exit):
    import urllib.error
    import urllib.request

    native_args.control_port = 0
    monkeypatch.setenv("FLAPSIM_CONTROL_FD", "999999")
    if early_exit:
        monkeypatch.setenv("FLAPSIM_FAKE_MODE", "exit")

    class Probe:
        timeout = 5
        document = {"version": 1, "mode": "boot", "timeout": 5}

        def execute(self, context):
            value = manifest(native_args)
            token = (context.out / "control-token").read_text().strip()
            assert token not in json.dumps(value)
            request = urllib.request.Request(
                value["control"]["endpoint"] + "/api/emu/state",
                headers={"Authorization": "Bearer " + token},
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with pytest.raises(urllib.error.HTTPError) as error:
                opener.open(request, timeout=2)
            assert error.value.code == 503
            return {}

    assert run(native_args, executor=Probe()) == (3 if early_exit else 0)
    value = manifest(native_args)
    assert value["control"]["status"] == "stopped"
    boot = json.loads((native_args.out / "boot-1.json").read_text())
    assert boot["control_type"] == socket.SOCK_SEQPACKET
    port = int(value["control"]["endpoint"].rsplit(":", 1)[1])
    with socket.socket() as connection:
        assert connection.connect_ex(("127.0.0.1", port)) != 0
    assert_reaped(value)


def test_native_disabled_control_strips_inherited_descriptor(native_args, monkeypatch):
    monkeypatch.setenv("FLAPSIM_CONTROL_FD", "999999")
    assert run(native_args) == 0
    boot = json.loads((native_args.out / "boot-1.json").read_text())
    assert boot["control_type"] is None
    assert "control" not in manifest(native_args)
    assert not (native_args.out / "control-token").exists()
