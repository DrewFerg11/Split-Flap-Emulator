"""Shared controlled runtime; real-firmware verification is separate from these fixtures."""

import argparse
import json
from pathlib import Path

import pytest

from flapsim.runner import QEMU_COMMIT

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
