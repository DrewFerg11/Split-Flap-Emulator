"""Exercise real subprocesses, listeners, signals and flash isolation."""

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from flapsim.executor import load_executor
from flapsim.hardware import load_hardware
from flapsim.process import enable_subreaper
from flapsim.runner import FLASH_SIZE, QEMU_COMMIT, command_for, run

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def runtime(tmp_path):
    prefix = tmp_path / "runtime"
    executable = prefix / "bin/flapsim-qemu"
    executable.parent.mkdir(parents=True)
    executable.write_text((ROOT / "tests/fixtures/fake_qemu.py").read_text())
    executable.chmod(0o755)
    for name in [
        "lib/libqemu-xtensa.so",
        "share/qemu/esp32-v3-rom.bin",
        "share/qemu/esp32-v3-rom-app.bin",
    ]:
        path = prefix / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    (prefix / "build-info.json").write_text(
        json.dumps(
            {
                "commit": QEMU_COMMIT,
                "integration_sha256": "a" * 64,
            }
        )
    )
    return prefix


@pytest.fixture
def arguments(tmp_path, runtime):
    firmware = tmp_path / "factory.bin"
    firmware.write_bytes(b"test firmware")
    return argparse.Namespace(
        firmware=firmware,
        hardware="esp32-8-single",
        scenario="boot",
        out=tmp_path / "run",
        timing="ci",
        seed=7,
        timeout=3,
        qemu_prefix=runtime,
        trace=False,
    )


def manifest(args):
    return json.loads((args.out / "run.json").read_text())


def assert_reaped(value):
    for attempt in value["attempts"]:
        assert isinstance(attempt["returncode"], int)
        with pytest.raises(ProcessLookupError):
            os.kill(attempt["pid"], 0)
        with pytest.raises(ChildProcessError):
            os.waitpid(attempt["pid"], os.WNOHANG)
        with socket.socket() as connection:
            assert connection.connect_ex(("127.0.0.1", attempt["port"])) != 0


@pytest.mark.parametrize("size", [1, 4128768, FLASH_SIZE])
def test_private_flash_boundaries_and_provenance(arguments, size):
    data = b"A" * size
    arguments.firmware.write_bytes(data)
    assert run(arguments) == 0
    value = manifest(arguments)
    assert value["passed"] and value["source_unchanged"]
    assert value["firmware"]["sha256"] == hashlib.sha256(data).hexdigest()
    assert arguments.firmware.read_bytes() == data
    observed = json.loads((arguments.out / "flash-observed.json").read_text())
    assert observed == {"size": FLASH_SIZE, "first": 65, "last": 65 if size == FLASH_SIZE else 255}
    # The child's persistent write stays in this working flash after cleanup.
    assert (arguments.out / "flash.bin").read_bytes()[-1:] == b"R"
    assert value["seed"] == 7 and value["build"]["commit"] == QEMU_COMMIT
    assert value["result"]["state"]["text"] == "XXXXXXXX"
    assert_reaped(value)


@pytest.mark.parametrize("size", [0, FLASH_SIZE + 1])
def test_reject_bad_flash_before_launch(arguments, size):
    arguments.firmware.write_bytes(b"A" * size)
    with pytest.raises(ValueError, match="firmware"):
        run(arguments)
    assert not arguments.out.exists()


@pytest.mark.parametrize(
    "mutation", ["stale", "file", "runtime", "hardware", "scenario", "timeout"]
)
def test_reject_invalid_run_before_launch(arguments, mutation):
    if mutation == "stale":
        arguments.out.mkdir()
        (arguments.out / "state.json").write_text("old success")
    elif mutation == "file":
        arguments.out.write_text("file")
    elif mutation == "runtime":
        (arguments.qemu_prefix / "lib/libqemu-xtensa.so").unlink()
    elif mutation == "hardware":
        arguments.hardware = "no-such-board"
    elif mutation == "scenario":
        arguments.scenario = "no-such-scenario"
    else:
        arguments.timeout = float("nan")
    with pytest.raises((ValueError, OSError)):
        run(arguments)
    assert not (arguments.out / "run.json").exists()


@pytest.mark.parametrize("timing,shift", [("ci", 3), ("accurate", 2), ("realtime", None)])
def test_command_timing_and_network_are_explicit(arguments, timing, shift):
    command = command_for(arguments.qemu_prefix, arguments.out, 12345, "/tmp/qmp", timing)
    assert "-accel" not in command and "thread=multi" not in " ".join(command)
    if shift is None:
        assert "-icount" not in command
    else:
        assert command[command.index("-icount") + 1] == f"shift={shift},sleep=off"
    assert "hostfwd=tcp:127.0.0.1:12345-192.168.4.1:80" in " ".join(command)
    assert "driver=timer.esp32.timg,property=wdt_disable,value=true" in command
    assert command[command.index("-monitor") + 1] == "none"


@pytest.mark.parametrize(
    "mode,code",
    [
        ("exit", 3),
        ("hang", 4),
        ("no-output", 4),
        ("stale", 4),
        ("partial", 3),
        ("fault", 5),
        ("bad-timeline", 3),
        ("tree-hang", 0),
        ("leader-exit", 3),
        ("slow-qmp", 0),
    ],
)
def test_runtime_failures_timeouts_and_descendant_cleanup(arguments, monkeypatch, mode, code):
    monkeypatch.setenv("FLAPSIM_FAKE_MODE", mode)
    arguments.timeout = 3
    before = time.monotonic()
    assert run(arguments) == code
    assert time.monotonic() - before < 8
    value = manifest(arguments)
    assert value["passed"] == (code == 0)
    assert_reaped(value)
    if (arguments.out / "descendant.pid").exists():
        descendant = int((arguments.out / "descendant.pid").read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(descendant, 0)


@pytest.mark.parametrize("mode,counter", [("fault", "stalls"), ("anomaly", "anomalies")])
def test_boot_faults_fail_even_when_expected_text_does_not_match(
    arguments, monkeypatch, mode, counter
):
    monkeypatch.setenv("FLAPSIM_FAKE_MODE", mode)
    scenario = arguments.out.parent / "fault.yaml"
    scenario.write_text(
        json.dumps({"version": 1, "mode": "boot", "timeout": 10, "expect_text": "YYYYYYYY"})
    )
    arguments.scenario = scenario
    arguments.timeout = 10
    assert run(arguments) == 5
    value = manifest(arguments)
    assert "model faults:" in value["error"] and f"'{counter}': 1" in value["error"]
    assert not value["passed"] and value["source_unchanged"]
    assert_reaped(value)


def test_runtime_failure_does_not_retry_a_bind_message_after_startup(arguments):
    class FailingExecutor:
        timeout = 10
        document = {"version": 1, "mode": "boot", "timeout": 10}

        def execute(self, context):
            log = max(
                context.out.glob("qemu-attempt-*.log"),
                key=lambda path: int(path.stem.rsplit("-", 1)[1]),
            )
            with log.open("a") as stream:
                stream.write("runtime diagnostic: Could not set up host forwarding rule\n")
            context.process.terminate()
            context.process.wait(timeout=5)
            raise RuntimeError("failure after QMP startup")

    arguments.timeout = 10
    assert run(arguments, executor=FailingExecutor()) == 3
    value = manifest(arguments)
    assert value["error"] == "failure after QMP startup"
    assert len(value["attempts"]) == 1 and "retry" not in value["attempts"][0]
    assert_reaped(value)


def test_source_tampering_after_launch_fails(arguments, monkeypatch):
    monkeypatch.setenv("FLAPSIM_FAKE_MODE", "tamper")
    monkeypatch.setenv("FLAPSIM_FAKE_SOURCE", str(arguments.firmware))
    assert run(arguments) == 3
    value = manifest(arguments)
    assert not value["source_unchanged"] and "changed" in value["error"]
    assert_reaped(value)


def test_exec_failure_is_recorded_without_a_child(arguments):
    (arguments.qemu_prefix / "bin/flapsim-qemu").write_bytes(b"invalid executable")
    assert run(arguments) == 3
    value = manifest(arguments)
    assert "Exec format" in value["error"]
    assert "pid" not in value["attempts"][0]


def test_port_retry_limit_is_bounded(arguments, monkeypatch):
    from flapsim import runner

    arguments.timeout = 10
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        monkeypatch.setattr(runner, "choose_port", lambda: occupied.getsockname()[1])
        assert run(arguments) == 3
    value = manifest(arguments)
    assert len(value["attempts"]) == 5
    assert_reaped(value)


def test_actual_port_bind_collision_retries(arguments, monkeypatch):
    from flapsim import runner

    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        original = runner.choose_port
        ports = iter([occupied.getsockname()[1], original()])
        monkeypatch.setattr(runner, "choose_port", lambda: next(ports))
        assert run(arguments) == 0
    value = manifest(arguments)
    assert len(value["attempts"]) == 2
    assert value["attempts"][0]["returncode"] == 1
    assert value["attempts"][0]["retry"] == "HTTP forwarding bind failed"
    assert_reaped(value)


def cli_command(args, *, timeout=10):
    return [
        sys.executable,
        "-m",
        "flapsim.cli",
        "run",
        "--firmware",
        str(args.firmware),
        "--hardware",
        args.hardware,
        "--scenario",
        "boot",
        "--out",
        str(args.out),
        "--qemu-prefix",
        str(args.qemu_prefix),
        "--timeout",
        str(timeout),
    ]


def stop_test_cli(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.communicate(timeout=10)
        return
    except subprocess.TimeoutExpired:
        # Only children of this test's CLI are eligible for forced cleanup.
        path = Path(f"/proc/{process.pid}/task/{process.pid}/children")
        children = [int(pid) for pid in path.read_text().split()] if path.exists() else []
        for pid in children:
            try:
                if os.getpgid(pid) == pid:
                    os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.kill()
        process.communicate(timeout=5)
        deadline = time.monotonic() + 2
        while children:
            for pid in children.copy():
                try:
                    reaped, _ = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    reaped = pid
                if reaped:
                    children.remove(pid)
            if children:
                if time.monotonic() >= deadline:
                    raise RuntimeError("test CLI children did not finish cleanup") from None
                time.sleep(0.02)


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_cli_signals_reap_children(arguments, signum):
    enable_subreaper()
    env = {**os.environ, "FLAPSIM_FAKE_MODE": "no-output"}
    process = subprocess.Popen(cli_command(arguments, timeout=30), env=env, stdout=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        while not (arguments.out / "flash-observed.json").exists():
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(0.02)
        process.send_signal(signum)
        process.communicate(timeout=10)
        assert process.returncode == 128 + signum
        assert_reaped(manifest(arguments))
    finally:
        stop_test_cli(process)


def test_two_concurrent_cli_instances_are_isolated(arguments, tmp_path):
    enable_subreaper()
    other = argparse.Namespace(**{**vars(arguments), "out": tmp_path / "other"})
    children = [
        subprocess.Popen(cli_command(a), stdout=subprocess.PIPE) for a in [arguments, other]
    ]
    try:
        for child in children:
            child.communicate(timeout=10)
            assert child.returncode == 0
        a, b = manifest(arguments), manifest(other)
        assert a["port"] != b["port"] and a["command"] != b["command"]
        assert (arguments.out / "flash.bin").stat().st_ino != (
            other.out / "flash.bin"
        ).stat().st_ino
        assert a["result"]["state"] == b["result"]["state"]
        assert_reaped(a)
        assert_reaped(b)
    finally:
        for child in children:
            stop_test_cli(child)


@pytest.mark.parametrize(
    "missing",
    [
        ("version",),
        ("mode",),
        ("timeout",),
        ("version", "mode"),
        ("version", "timeout"),
        ("mode", "timeout"),
        ("version", "mode", "timeout"),
    ],
)
def test_cli_missing_boot_fields_fail_before_launch(arguments, tmp_path, missing):
    document = {"version": 1, "mode": "boot", "timeout": 2}
    scenario = tmp_path / "incomplete.yaml"
    scenario.write_text(
        json.dumps({key: value for key, value in document.items() if key not in missing})
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "flapsim.cli",
            "run",
            "--firmware",
            str(arguments.firmware),
            "--hardware",
            arguments.hardware,
            "--scenario",
            str(scenario),
            "--out",
            str(arguments.out),
            "--qemu-prefix",
            str(arguments.qemu_prefix),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 2
    assert "Invalid run: boot scenario:" in completed.stderr
    assert "missing [" in completed.stderr
    assert all(field in completed.stderr for field in missing)
    assert "Traceback" not in completed.stderr
    assert not arguments.out.exists()


def test_boot_document_rejects_unknown_fields_and_invalid_expectation(tmp_path):
    path = tmp_path / "scenario.yaml"
    hardware = load_hardware("esp32-8-single")
    for text in [
        "version: 1\nmode: boot\ntimeout: .inf\n",
        "version: 1\nmode: boot\ntimeout: 2\nextra: yes\n",
        "version: 1\nmode: boot\ntimeout: 2\nexpect_text: X\n",
        "version: 1\nmode: boot\ntimeout: 2\nmode: boot\n",
    ]:
        path.write_text(text)
        with pytest.raises(ValueError):
            load_executor(path, hardware)
