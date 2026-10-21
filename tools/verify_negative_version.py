"""Assert real CLI failures for a prerelease of the golden tag and a deliberately wrong version."""

import argparse
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from tools.verify_boot import GOLDEN, ROOT, checked_firmware


def verify(args):
    checked_firmware(args.firmware)
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError("negative evidence directory must be fresh")
    out.mkdir(parents=True, exist_ok=True)
    cases = []
    # The rc case is the v1.4.0 failure mode: a stable build must not satisfy its own prerelease.
    reported = f"Firmware: {GOLDEN['tag']} ("
    for name, version in [("rc-tag", f"{GOLDEN['tag']}-rc.1"), ("wrong-version", "v9.9.9")]:
        case = out / name
        command = [
            sys.executable,
            "-m",
            "flapsim.cli",
            "run",
            "--firmware",
            str(args.firmware),
            "--hardware",
            "esp32-8-single",
            "--scenario",
            str(ROOT / "scenarios/version-check.yaml"),
            "--out",
            str(case),
            "--qemu-prefix",
            str(args.qemu_prefix),
            "--var",
            f"FIRMWARE_VERSION={version}",
        ]
        completed = subprocess.run(command, check=False)
        manifest = json.loads((case / "run.json").read_text())
        if (
            completed.returncode != 5
            or manifest["passed"] is not False
            or manifest["source_unchanged"] is not True
            or manifest["steps"][0]["status"] != "passed"
            or manifest["steps"][1]["status"] != "failed"
            or reported not in manifest["steps"][1].get("actual", "")
            or manifest.get("version_exception") is not None
        ):
            raise RuntimeError(
                f"negative {name} did not fail for its version expectation: {manifest}"
            )
        if ET.parse(case / "junit.xml").getroot().get("failures") != "1":
            raise RuntimeError("negative run did not produce a failing JUnit case")
        for artifact in ["report.md", "replay.html"]:
            if not (case / artifact).is_file():
                raise RuntimeError(f"negative run missing {artifact}")
        for attempt in manifest["attempts"]:
            try:
                os.kill(attempt["pid"], 0)
            except ProcessLookupError:
                pass
            else:
                raise RuntimeError("negative CLI run left its emulator alive")
        cases.append(
            {
                "case": name,
                "expected_version": version,
                "exit_code": completed.returncode,
                "wall_seconds": manifest["wall_seconds"],
                "report": str(case / "report.md"),
            }
        )
    (out / "verification.json").write_text(
        json.dumps({"passed": True, "cases": cases}, indent=2) + "\n"
    )
    print("PASS: golden prerelease tag and deliberately wrong version both fail with evidence")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--firmware", type=Path, default=ROOT / "firmware" / GOLDEN["asset"])
    parser.add_argument("--qemu-prefix", type=Path, default=ROOT / "build/qemu/install")
    args = parser.parse_args()
    try:
        verify(args)
    except Exception as error:
        print(f"Negative-version verification failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
