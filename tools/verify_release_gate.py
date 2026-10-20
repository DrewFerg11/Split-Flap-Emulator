"""Run a same-run release factory artifact through a digest-pinned, strict container gate.

Standard library only: the firmware workflow checks out this helper at an immutable
emulator commit. A failed probe still exits nonzero; a separate dry-run assertion
checks the retained evidence and the failed publication dependency.
"""

import argparse
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

IMAGE_PATTERN = r"ghcr\.io/drewferg11/flapsim@sha256:[0-9a-f]{64}"
TAG_PATTERN = r"v\d+\.\d+\.\d+(?:-rc\.\d+)?"
LAYOUTS = {"8": ("esp32-8-single", "release-smoke-8"), "16": ("esp32-16-dual", "release-smoke-16")}
PROBES = ("none", "wrong-version", "missing", "corrupt", "timeout")
WRONG_VERSION = "v0.0.0-probe-mismatch"


class GateFailure(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def validate_inputs(image, version):
    if not re.fullmatch(IMAGE_PATTERN, image):
        raise GateFailure("configuration", "FLAPSIM_IMAGE must be the verified GHCR sha256 digest")
    if not re.fullmatch(TAG_PATTERN, version):
        raise GateFailure("configuration", "expected an authorized vX.Y.Z or vX.Y.Z-rc.N tag")


def read_factory(path):
    if not path.is_file():
        raise GateFailure("missing", f"same-run factory artifact is missing: {path.name}")
    if path.is_symlink() or not 65536 < path.stat().st_size <= 4 * 1024 * 1024:
        raise GateFailure("corrupt", "factory image must be a regular ESP32 image <=4MiB")
    data = path.read_bytes()
    # This workflow's merged factory is addressed from zero, with ESP headers at
    # the ESP32 bootloader/app offsets. An app-only image is not a factory image.
    if data[0x1000] != 0xE9 or data[0x10000] != 0xE9:
        raise GateFailure("corrupt", "factory image is missing ESP bootloader/application headers")
    return hashlib.sha256(data).hexdigest()


def strict_command(layout, version, probe):
    hardware, scenario = LAYOUTS[layout]
    command = [
        "run",
        "--firmware",
        "/firmware.bin",
        "--hardware",
        hardware,
        "--scenario",
        "boot" if probe == "timeout" else scenario,
        "--out",
        "/out/run",
        "--timing",
        "ci",
        "--timeout",
        "3" if probe == "timeout" else "300",
    ]
    if probe != "timeout":
        expected = WRONG_VERSION if probe == "wrong-version" else version
        command += ["--var", f"FIRMWARE_VERSION={expected}"]
    return command


def inspect_result(out, code, checksum, layout, probe):
    manifest = json.loads((out / "run/run.json").read_text())
    if (
        manifest.get("firmware", {}).get("sha256") != checksum
        or manifest.get("source_unchanged") is not True
        or manifest.get("exit_code") != code
        or manifest.get("cleanup_error")
        or manifest.get("known_issues") is not None
        or manifest.get("version_exception") is not None
        or not manifest.get("attempts")
        or any(type(a.get("returncode")) is not int for a in manifest["attempts"])
        or type(manifest.get("qemu_returncode")) is not int
        or manifest["qemu_returncode"] != manifest["attempts"][-1]["returncode"]
    ):
        raise GateFailure("evidence", "inconsistent provenance, cleanup or strict-policy evidence")
    for name in ("serial.log", "timeline.jsonl", "report.md", "junit.xml", "replay.html"):
        if not (out / "run" / name).is_file():
            raise GateFailure("evidence", f"missing simulator artifact: {name}")
    junit = ET.parse(out / "run/junit.xml").getroot()
    if code:
        if manifest.get("passed") is not False or manifest.get("status") != "failed":
            raise GateFailure("evidence", "nonzero execution must have a failed manifest")
        if (
            code == 4
            and probe == "timeout"
            and "deadline" in manifest.get("error", "")
            and manifest.get("scenario", {}).get("mode") == "boot"
            and manifest.get("timeout_seconds") == 3
            and not manifest.get("steps")
            and int(junit.get("failures", "0")) > 0
        ):
            raise GateFailure("timeout", manifest["error"])
        if code == 5 and probe == "wrong-version":
            for step in manifest.get("steps", []):
                if (
                    step.get("action") == "expect_serial"
                    and step.get("status") == "failed"
                    and WRONG_VERSION in str(step.get("expected"))
                    and "Firmware: " in str(step.get("actual"))
                    and int(junit.get("failures", "0")) > 0
                    and manifest.get("qemu_returncode") == 0
                ):
                    raise GateFailure("wrong-version", manifest.get("error", "version mismatch"))
        raise GateFailure("simulation", manifest.get("error", f"simulator exited {code}"))
    steps = manifest.get("steps", [])
    if (
        manifest.get("passed") is not True
        or manifest.get("status") != "passed"
        or manifest.get("qemu_returncode") != 0
        or manifest.get("scenario", {}).get("name") != LAYOUTS[layout][1]
        or not steps
        or any(s.get("status") != "passed" for s in steps)
        or any(int(junit.get(k, "0")) for k in ("failures", "errors", "skipped"))
    ):
        raise GateFailure("evidence", "success requires complete, strict passing steps and JUnit")
    return manifest


def bind_mount(source, target, *, readonly=False):
    value = io.StringIO()
    fields = ["type=bind", f"source={source}", f"target={target}"]
    csv.writer(value, lineterminator="").writerow(fields + (["readonly"] if readonly else []))
    return value.getvalue()


def verify(args):
    started = time.monotonic()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    result = {
        "passed": False,
        "image": args.image,
        "version": args.version,
        "layout": args.layout,
        "probe": args.probe,
        "error_kind": None,
    }
    name = "flapsim-release-" + uuid.uuid4().hex
    created = False
    original = None
    source = args.artifact_dir.resolve() / f"splitflap-esp32_n4-{args.version}-factory.bin"

    def docker(command, timeout=30):
        with (out / "docker.log").open("a") as log:
            return subprocess.run(
                ["docker", *command],
                text=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            ).returncode

    try:
        validate_inputs(args.image, args.version)
        original = read_factory(source)
        result["firmware_sha256"] = original
        target = source
        if args.probe == "missing":
            target = out / "deliberately-missing.bin"
        elif args.probe == "corrupt":
            target = out / "deliberately-corrupt.bin"
            target.write_bytes(b"deliberately corrupt dry-run artifact")
        checksum = read_factory(target)
        if docker(["pull", "--platform", "linux/amd64", args.image], timeout=180):
            raise GateFailure("image", "cannot pull the configured immutable image")
        command = strict_command(args.layout, args.version, args.probe)
        result["command"] = command
        # Track the name before create: cleanup also covers a create client timeout.
        created = True
        if docker(
            [
                "create",
                "--name",
                name,
                "--platform",
                "linux/amd64",
                "--network",
                "none",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--mount",
                bind_mount(target, "/firmware.bin", readonly=True),
                "--mount",
                bind_mount(out, "/out"),
                args.image,
                *command,
            ]
        ):
            raise GateFailure("container", "cannot create the isolated simulator")
        code = docker(["start", "--attach", name], timeout=360)
        result["simulator_exit_code"] = code
        manifest = inspect_result(out, code, checksum, args.layout, args.probe)
        result["simulator_wall_seconds"] = manifest["wall_seconds"]
        result["passed"] = True
    except GateFailure as error:
        result.update(error_kind=error.kind, error=str(error))
    except subprocess.TimeoutExpired:
        result.update(
            error_kind="infrastructure-timeout", error="bounded Docker operation timed out"
        )
    except Exception as error:
        result.update(error_kind="evidence", error=str(error))
    finally:
        if created:
            try:
                if docker(["rm", "--force", name]):
                    result.update(
                        passed=False, error_kind="cleanup", error="container removal failed"
                    )
            except Exception as error:
                result.update(passed=False, error_kind="cleanup", error=str(error))
        if original is not None:
            try:
                if hashlib.sha256(source.read_bytes()).hexdigest() != original:
                    raise ValueError("same-run artifact changed")
            except (OSError, ValueError) as error:
                result.update(passed=False, error_kind="integrity", error=str(error))
        result["wall_seconds"] = round(time.monotonic() - started, 3)
        (out / "release-gate.json").write_text(json.dumps(result, indent=2) + "\n")
        summary = "Release simulation: " + json.dumps(result, indent=2) + "\n"
        (out / "summary.md").write_text("```json\n" + summary + "```\n")
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as stream:
                stream.write((out / "summary.md").read_text())
        print(summary)
    return 0 if result["passed"] else 1


def assert_probe(directory, probe, sim_result, eligibility_result):
    if probe == "none" or sim_result != "failure" or eligibility_result != "failure":
        raise ValueError("negative dry run must fail simulation AND publication eligibility")
    for layout in LAYOUTS:
        path = directory / f"release-sim-{layout}" / "evidence" / "release-gate.json"
        result = json.loads(path.read_text())
        if (
            result.get("passed") is not False
            or result.get("probe") != probe
            or result.get("layout") != layout
            or result.get("error_kind") != probe
            or not re.fullmatch(r"[0-9a-f]{64}", result.get("firmware_sha256", ""))
        ):
            raise ValueError(f"layout {layout} did not prove the intended {probe} rejection")
    print(f"PASS: both layouts rejected {probe}; publication eligibility failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run")
    run.add_argument("--image", required=True)
    run.add_argument("--version", required=True)
    run.add_argument("--artifact-dir", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--layout", choices=LAYOUTS, required=True)
    run.add_argument("--probe", choices=PROBES, default="none")
    assertion = sub.add_parser("assert-probe")
    assertion.add_argument("--directory", type=Path, required=True)
    assertion.add_argument("--probe", choices=PROBES, required=True)
    assertion.add_argument("--sim-result", required=True)
    assertion.add_argument("--eligibility-result", required=True)
    args = parser.parse_args()
    if args.action == "run":
        return verify(args)
    try:
        assert_probe(args.directory, args.probe, args.sim_result, args.eligibility_result)
    except (OSError, ValueError) as error:
        print(f"Dry-run rejection assertion failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
