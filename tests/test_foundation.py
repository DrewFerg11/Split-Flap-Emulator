"""Negative checks for the build boundary and real-boot evidence contract."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools import verify_boot

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("qemu_build", ROOT / "qemu/build.py")
qemu_build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qemu_build)


def test_cli_installed():
    result = subprocess.run(["flapsim", "--help"], capture_output=True, text=True, check=True)
    assert "virtual ESP32 hardware" in result.stdout


def test_missing_build_dependency(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "qemu/build.py"), "--build-root", str(tmp_path / "build")],
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "missing build dependency: git" in result.stderr
    assert not (tmp_path / "build").exists()


@pytest.mark.parametrize("jobs", ["0", "-1"])
def test_invalid_build_jobs(jobs, tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "qemu/build.py"),
            "--jobs",
            jobs,
            "--build-root",
            str(tmp_path / "build"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "--jobs must be positive" in result.stderr
    assert not (tmp_path / "build").exists()


def test_shared_link_preserves_flags_and_omits_main(tmp_path):
    (tmp_path / "qemu-system-xtensa.rsp").write_text(
        "-o qemu-system-xtensa qemu-system-xtensa.p/system_main.c.o "
        '-Wl,--start-group "lib with spaces.a" -lgcrypt -lslirp -Wl,--end-group'
    )
    command = qemu_build.shared_link_command("cc -m64 @qemu-system-xtensa.rsp", tmp_path)
    assert command == [
        "cc",
        "-m64",
        "-o",
        "libqemu-xtensa.so",
        "-Wl,--start-group",
        "lib with spaces.a",
        "-lgcrypt",
        "-lslirp",
        "-Wl,--end-group",
        "-shared",
    ]


def test_shared_link_fails_closed_on_abi_build_change(tmp_path):
    with pytest.raises(ValueError, match="exactly one system main object"):
        qemu_build.shared_link_command("cc -o qemu-system-xtensa libqemu.a", tmp_path)


def test_banner_or_callback_alone_is_not_boot_proof():
    banner = "=== Split Flap Display ===\n"
    discovery = "Wire  : [0]0x20\n"
    callback = "FLAPSIM probe: bus=0 address=0x20 ACK event=1\n"
    assert verify_boot.boot_evidence(banner + discovery, callback)
    assert not verify_boot.boot_evidence(banner, callback)
    assert not verify_boot.boot_evidence(discovery, callback)
    assert not verify_boot.boot_evidence(banner + discovery, "")
    assert not verify_boot.boot_evidence(banner + discovery.replace("0x20", "0x21"), callback)
    assert not verify_boot.boot_evidence(banner + "Wire (SDA=21, SCL=22): [0]0x20", callback)


def test_corrupt_golden_firmware_is_rejected(tmp_path):
    firmware = tmp_path / "factory.bin"
    firmware.write_bytes(b"bad")
    with pytest.raises(ValueError, match="golden firmware mismatch"):
        verify_boot.fetch_firmware(firmware)
    assert firmware.read_bytes() == b"bad"


def fake_golden(monkeypatch, firmware):
    data = b"test factory image"
    firmware.write_bytes(data)
    monkeypatch.setitem(verify_boot.GOLDEN, "size", len(data))
    monkeypatch.setitem(verify_boot.GOLDEN, "sha256", hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize("mode", ["exit", "timeout", "banner_only", "pass"])
def test_boot_supervision_and_retained_evidence(tmp_path, monkeypatch, mode):
    firmware = tmp_path / "factory.bin"
    fake_golden(monkeypatch, firmware)
    prefix = tmp_path / "qemu"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "build-info.json").write_text("{}")
    launcher = prefix / "bin/flapsim-qemu"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, sys, time\n"
        f"mode = {mode!r}\n"
        "serial = pathlib.Path(sys.argv[sys.argv.index('-serial') + 1][5:])\n"
        "serial.with_suffix('.pid').write_text(str(os.getpid()))\n"
        "serial.write_text('' if mode == 'timeout' else '=== Split Flap Display ===\\n' + "
        "('Wire  : [0]0x20\\n' if mode == 'pass' else ''))\n"
        "print('FLAPSIM probe: bus=0 address=0x20 ACK event=1', flush=True)\n"
        "if mode != 'exit': time.sleep(60)\n"
    )
    launcher.chmod(0o755)
    out = tmp_path / "run"
    args = type(
        "Args",
        (),
        {
            "out": out,
            "firmware": firmware,
            "qemu_prefix": prefix,
            "timeout": 3.0,
        },
    )()
    if mode == "pass":
        verify_boot.verify(args)
    else:
        with pytest.raises((RuntimeError, TimeoutError)):
            verify_boot.verify(args)
    evidence = json.loads((out / "verification.json").read_text())
    assert evidence["passed"] == (mode == "pass")
    assert (out / "serial.log").exists()
    assert (out / "qemu.log").exists()
    assert (out / "flash.bin").stat().st_size == verify_boot.FLASH_SIZE
    assert firmware.read_bytes() == b"test factory image"
    assert evidence["wall_seconds"] < 10
    pid = int((out / "serial.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_stale_output_rejected(tmp_path):
    args = type("Args", (), {"out": tmp_path, "timeout": 1})()
    (tmp_path / "serial.log").write_text("old evidence")
    with pytest.raises(ValueError, match="must be empty"):
        verify_boot.verify(args)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_unbounded_timeout_rejected(timeout):
    args = type("Args", (), {"timeout": timeout})()
    with pytest.raises(ValueError, match="finite and positive"):
        verify_boot.verify(args)
