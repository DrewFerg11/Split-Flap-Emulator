"""Bounded phase-2 real-firmware model check; phase 3 replaces supervision with the CLI."""

import argparse
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from flapsim.hardware import load_hardware, write_model_config
from flapsim.state import read_settlement
from tools.verify_boot import FLASH_SIZE, GOLDEN, checked_firmware, fetch_firmware, stop

ROOT = Path(__file__).resolve().parents[1]


def check_serial_errors(serial, hardware):
    """Only the pinned fresh-flash golden boot's two known errors are allowed."""
    present = {(m["bus"], m["addr"]) for m in hardware["modules"]}
    expected_writes = sorted(a for b in range(2) for a in range(32, 40) if (b, a) not in present)
    actual_writes = []
    for line in serial.splitlines():
        if re.fullmatch(
            r"\[\s*\d+\]\[E\]\[Preferences.cpp:50\] begin\(\): nvs_open failed: NOT_FOUND", line
        ):
            continue
        missing_write = re.fullmatch(r"Error writing data to module (\d+), error code: 2", line)
        if missing_write:
            actual_writes.append(int(missing_write[1]))
        elif re.search(r"\[E\]|\b(error|failed|panic|abort|exception)\b", line, re.IGNORECASE):
            raise RuntimeError(f"unexpected firmware serial error: {line}")
    if sorted(actual_writes) != expected_writes:
        raise RuntimeError(f"unexpected missing-module write errors: {actual_writes}")


def verify(args):
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("--timeout must be finite and positive")
    hardware = load_hardware(args.hardware)
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError("--out must be empty")
    out.mkdir(parents=True, exist_ok=True)
    result = {"passed": False, "firmware": GOLDEN, "hardware": hardware}
    started = time.monotonic()
    try:
        fetch_firmware(args.firmware)
        data = checked_firmware(args.firmware)
        flash = out / "flash.bin"
        flash.write_bytes(data + b"\xff" * (FLASH_SIZE - len(data)))
        config = write_model_config(hardware, out)
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
                env={**os.environ, "FLAPSIM_CONFIG": str(config)},
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
                            f"emulator exited before model verification: {process.returncode}"
                        )
                    serial_path = out / "serial.log"
                    serial = serial_path.read_text(errors="replace") if serial_path.exists() else ""
                    state = read_settlement(out, hardware)
                    if state is not None:
                        result["last_state"] = state
                    if (
                        state
                        and state["movement_id"] > 0
                        and state["text"] == "X" * len(hardware["modules"])
                        and "=== Split Flap Display ===" in serial
                        and serial.endswith("\n")
                        and re.search(r"(?m)^Wire\s*:\s+\[0\]0x20(?:\s|$)", serial)
                    ):
                        counters = json.loads((out / "activity.json").read_text())["counters"]
                        if counters["stalls"] or counters["anomalies"]:
                            raise RuntimeError(f"model regression: {counters}")
                        # Golden firmware's defaults list eight modules on both buses.
                        missing = sorted(
                            int(n) for n in re.findall(r"-> \[(\d+)\]0x[0-9A-F]+ MISSING", serial)
                        )
                        present = {m["bus"] * 8 + m["addr"] - 32 for m in hardware["modules"]}
                        if missing != sorted(set(range(16)) - present):
                            raise RuntimeError(f"unexpected firmware discovery failures: {missing}")
                        check_serial_errors(serial, hardware)
                        result["serial_checked_lines"] = len(serial.splitlines())
                        result["serial_checked_tail"] = serial.splitlines()[-10:]
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "firmware did not produce the expected fresh settled X display"
                        )
                    time.sleep(0.1)
            finally:
                try:
                    stop(process)
                finally:
                    result["qemu_returncode"] = process.poll()
            checked_firmware(args.firmware)
            result["passed"] = True
    except BaseException as error:
        result["error"] = str(error) or type(error).__name__
        raise
    finally:
        result["wall_seconds"] = round(time.monotonic() - started, 3)
        (out / "verification.json").write_text(json.dumps(result, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hardware", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--firmware", type=Path, default=ROOT / "firmware" / GOLDEN["asset"])
    parser.add_argument("--qemu-prefix", type=Path, default=ROOT / "build/qemu/install")
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args()

    def terminated(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminated)
    try:
        verify(args)
    except (ValueError, OSError, RuntimeError, KeyboardInterrupt) as error:
        print(f"Model verification failed: {error}", file=sys.stderr)
        return 1
    print(f"PASS: {args.hardware}, settled X display, zero stalls/anomalies; evidence: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
