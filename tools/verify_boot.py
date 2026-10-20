#!/usr/bin/env python3
"""Bounded phase-1 verification; replaced by the runner/scenarios in phases 3/4."""

import argparse
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLASH_SIZE = 4 * 1024 * 1024
GOLDEN = json.loads((Path(__file__).with_name("golden_firmware.json")).read_text())


def checked_firmware(path: Path) -> bytes:
    data = path.read_bytes()
    checksum = hashlib.sha256(data).hexdigest()
    if len(data) != GOLDEN["size"] or checksum != GOLDEN["sha256"]:
        raise ValueError(f"golden firmware mismatch: size={len(data)}, sha256={checksum}")
    return data


def fetch_firmware(path: Path) -> None:
    if path.exists():
        checked_firmware(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Bound both network time and input size; publish only after checksum validation.
    with urllib.request.urlopen(GOLDEN["url"], timeout=60) as response:
        data = response.read(FLASH_SIZE + 1)
    if len(data) != GOLDEN["size"] or hashlib.sha256(data).hexdigest() != GOLDEN["sha256"]:
        raise ValueError("downloaded firmware does not match tools/golden_firmware.json")
    temporary = path.with_suffix(".download")
    temporary.write_bytes(data)
    temporary.replace(path)


def boot_evidence(serial: str, qemu: str) -> bool:
    return (
        "=== Split Flap Display ===" in serial
        and re.search(r"(?m)^Wire\s*:\s+\[0\]0x20(?:\s|$)", serial) is not None
        and "FLAPSIM probe: bus=0 address=0x20 ACK" in qemu
    )


def stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=5)
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def verify(args) -> None:
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("--timeout must be finite and positive")
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError("--out must be empty (old logs must not satisfy the boot assertion)")
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    result = {"passed": False, "firmware": GOLDEN}
    try:
        fetch_firmware(args.firmware)
        data = checked_firmware(args.firmware)
        flash = out / "flash.bin"
        if flash.resolve() == args.firmware.resolve():
            raise ValueError("firmware input cannot be the working flash path")
        flash.write_bytes(data + b"\xff" * (FLASH_SIZE - len(data)))
        prefix = args.qemu_prefix.resolve()
        result["build"] = json.loads((prefix / "build-info.json").read_text())
        command = [
            str(prefix / "bin/flapsim-qemu"),
            "-M",
            "esp32-picsimlab",
            "-L",
            str(prefix / "share/qemu"),
            "-drive",
            f"file={flash},if=mtd,format=raw",
            "-nographic",
            "-monitor",
            "none",
            "-serial",
            f"file:{out / 'serial.log'}",
            "-nic",
            "user,model=esp32_wifi,net=192.168.4.0/24",
            "-global",
            "driver=timer.esp32.timg,property=wdt_disable,value=true",
            "-icount",
            "shift=3,sleep=off",
        ]
        result["command"] = command
        with (out / "qemu.log").open("wb") as log:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + args.timeout
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(
                            f"emulator exited before verification completed: {process.returncode}"
                        )
                    serial = out / "serial.log"
                    if boot_evidence(
                        serial.read_text(errors="replace") if serial.exists() else "",
                        (out / "qemu.log").read_text(errors="replace"),
                    ):
                        # Recheck the source after the guest has been allowed to write to flash.
                        checked_firmware(args.firmware)
                        result["passed"] = True
                        return
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"no firmware banner + 0x20 discovery after {args.timeout}s"
                        )
                    time.sleep(0.1)
            finally:
                stop(process)
                result["qemu_returncode"] = process.returncode
    except BaseException as error:
        result["error"] = str(error) or type(error).__name__
        raise
    finally:
        result["wall_seconds"] = round(time.monotonic() - started, 3)
        (out / "verification.json").write_text(json.dumps(result, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware", type=Path, default=ROOT / "firmware" / GOLDEN["asset"])
    parser.add_argument("--qemu-prefix", type=Path, default=ROOT / "build/qemu/install")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()

    def terminated(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminated)
    try:
        verify(args)
    except (ValueError, OSError, RuntimeError, TimeoutError, KeyboardInterrupt) as error:
        print(f"Boot verification failed: {error}", file=sys.stderr)
        return 1
    print(f"PASS: firmware banner, 0x20 discovery and callback ACK; evidence: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
