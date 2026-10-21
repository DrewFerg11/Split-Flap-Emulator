"""Fetch the pinned golden asset or inspect evidence from a completed CLI run."""

import argparse
import json
import re
import sys
from pathlib import Path

from flapsim.hardware import load_hardware
from flapsim.state import display_aligned, read_settlement
from tools.verify_boot import GOLDEN, checked_firmware, fetch_firmware

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


def verify_existing(args):
    """Check golden-specific evidence after the CLI has stopped and reaped QEMU."""
    out = args.out.resolve()
    result = {"passed": False, "firmware": GOLDEN}
    try:
        checked_firmware(args.firmware)
        manifest = json.loads((out / "run.json").read_text())
        if (
            manifest.get("passed") is not True
            or manifest.get("exit_code") != 0
            or manifest.get("source_unchanged") is not True
            or manifest.get("qemu_returncode") != 0
            or manifest.get("firmware", {}).get("sha256") != GOLDEN["sha256"]
        ):
            raise RuntimeError("CLI did not record a clean verified golden run")
        hardware = load_hardware(args.hardware)
        if manifest["hardware"] != hardware:
            raise RuntimeError("CLI used a different hardware configuration")
        state = read_settlement(out, hardware)
        if (
            not state
            or state["movement_id"] <= 0
            or state["text"] != "X" * len(hardware["modules"])
            or not display_aligned(state, state["text"])
        ):
            raise RuntimeError("missing fresh golden startup settlement")
        serial = (out / "serial.log").read_text(errors="replace")
        # Stopping QEMU can leave one unfinished serial line; only complete lines are evidence.
        serial = serial[: serial.rfind("\n") + 1]
        if "=== Split Flap Display ===" not in serial or not re.search(
            r"(?m)^Wire\s*:\s+\[0\]0x20(?:\s|$)", serial
        ):
            raise RuntimeError("missing firmware banner or actual module discovery")
        counters = json.loads((out / "activity.json").read_text())["counters"]
        if counters["stalls"] or counters["anomalies"]:
            raise RuntimeError(f"model regression: {counters}")
        missing = sorted(int(n) for n in re.findall(r"-> \[(\d+)\]0x[0-9A-F]+ MISSING", serial))
        present = {m["bus"] * 8 + m["addr"] - 32 for m in hardware["modules"]}
        if missing != sorted(set(range(16)) - present):
            raise RuntimeError(f"unexpected firmware discovery failures: {missing}")
        check_serial_errors(serial, hardware)
        result.update(
            passed=True,
            last_state=state,
            wall_seconds=manifest["wall_seconds"],
            build=manifest["build"],
            qemu_returncode=manifest["qemu_returncode"],
            serial_checked_lines=len(serial.splitlines()),
            serial_checked_tail=serial.splitlines()[-10:],
        )
    except Exception as error:
        result["error"] = str(error) or type(error).__name__
        raise
    finally:
        (out / "verification.json").write_text(json.dumps(result, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hardware")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--firmware", type=Path, default=ROOT / "firmware" / GOLDEN["asset"])
    parser.add_argument(
        "--fetch-only", action="store_true", help="fetch and checksum the golden asset"
    )
    args = parser.parse_args()
    if not args.fetch_only and (not args.out or not args.hardware):
        parser.error("--out and --hardware are required when checking CLI evidence")
    try:
        if args.fetch_only:
            fetch_firmware(args.firmware)
        else:
            verify_existing(args)
    except Exception as error:
        print(f"Golden verification failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
