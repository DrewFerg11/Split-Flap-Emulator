"""Exercise the real C launcher against a controlled shared-library ABI."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def launcher(tmp_path):
    prefix = tmp_path / "prefix with spaces"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "lib").mkdir()
    executable = prefix / "bin/flapsim-qemu"
    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-I",
            str(ROOT / "qemu"),
            str(ROOT / "qemu/launcher.c"),
            str(ROOT / "tests/fixtures/model_stub.c"),
            "-ldl",
            "-o",
            str(executable),
        ],
        check=True,
    )
    return executable


@pytest.mark.parametrize("missing_symbol", [False, True])
def test_registration_lifecycle_and_loader_errors(launcher, missing_symbol):
    library = launcher.parent.parent / "lib/libqemu-xtensa.so"
    command = [
        "cc",
        "-std=c11",
        "-fPIC",
        "-shared",
        "-pthread",
        "-I",
        str(ROOT / "qemu"),
        str(ROOT / "tests/fixtures/qemu_abi_stub.c"),
        "-o",
        str(library),
    ]
    if missing_symbol:
        command.append("-DOMIT_CLEANUP")
    subprocess.run(command, check=True)
    result = subprocess.run([str(launcher), "--fixture"], capture_output=True, text=True)
    if missing_symbol:
        assert result.returncode == 1
        assert "missing symbol qemu_cleanup" in result.stderr
        assert result.stdout == ""
    else:
        assert result.returncode == 23
        assert result.stdout.splitlines() == ["register", "initialize", "main_loop", "cleanup"]


def test_missing_library_fails_with_diagnostic(launcher):
    result = subprocess.run([str(launcher), "--fixture"], capture_output=True, text=True)
    assert result.returncode == 1
    assert "flapsim-qemu:" in result.stderr
    assert "libqemu-xtensa.so" in result.stderr
