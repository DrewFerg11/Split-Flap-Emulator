"""Verify the exact local image with external firmware/output mounts and no source checkout."""

import argparse
import contextlib
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

from flapsim.executor import positive_timeout
from flapsim.runner import QEMU_COMMIT
from tools.verify_boot import GOLDEN, ROOT, checked_firmware

RESOURCE_CHECK = """
import json, pathlib, tarfile
from importlib.metadata import version
from flapsim.hardware import load_hardware
from flapsim.provenance import emulator_provenance
from flapsim.resources import resolve_resource
assert not pathlib.Path('/src').exists(), 'source checkout must not be mounted'
assert '/site-packages/' in str(pathlib.Path(__import__('flapsim').__file__))
hardware = {name: load_hardware(name) for name in ['esp32-8-single','esp32-16-dual']}
assert len(hardware['esp32-8-single']['modules']) == 8
assert len(hardware['esp32-16-dual']['modules']) == 16
scenarios = {name: resolve_resource('scenarios',name).read_text() for name in
             ['release-smoke-8','release-smoke-16','homing-random','version-check']}
sources = pathlib.Path('/usr/share/doc/flapsim/sources')
for name, member in [('qemu.tar.xz','./configure'),('flapsim.tar.xz','./qemu/build.py')]:
    with tarfile.open(sources/name) as archive:
        assert archive.getmember(member).isfile()
assert 'GNU GENERAL PUBLIC LICENSE' in pathlib.Path('/usr/share/doc/flapsim/LICENSE').read_text()
print(json.dumps({'version':version('flapsim'),'provenance':emulator_provenance(),
                  'hardware':hardware,'scenarios':scenarios}))
"""


def mount(source, target, *, readonly=False):
    result = io.StringIO()
    fields = ["type=bind", f"source={source}", f"target={target}"]
    if readonly:
        fields.append("readonly")
    csv.writer(result, lineterminator="").writerow(fields)
    return result.getvalue()


class ImageVerifier:
    def __init__(self, args, out):
        self.args, self.out = args, out
        self.docker = ["docker"] + (["--host", args.docker_host] if args.docker_host else [])
        info = json.loads(self.call(["image", "inspect", args.image]))[0]
        if info["Architecture"] != "amd64" or info["Os"] != "linux":
            raise ValueError("only Linux amd64 container verification is supported")
        if info["Config"]["Entrypoint"] != ["flapsim"]:
            raise ValueError("runtime entrypoint must be flapsim")
        self.image = info["Id"]
        self.identity = {k: info[k] for k in ["Id", "Architecture", "Os", "Created"]}
        self.identity["labels"] = info["Config"].get("Labels", {})
        resource_container = self.call(
            [
                "create",
                "--name",
                f"flapsim-resources-{uuid.uuid4().hex}",
                "--network",
                "none",
                "--entrypoint",
                "/opt/flapsim/venv/bin/python",
                self.image,
                "-c",
                RESOURCE_CHECK,
            ]
        ).strip()
        try:
            self.resources = json.loads(self.call(["start", "--attach", resource_container]))
        finally:
            self.call(["rm", "--force", resource_container], timeout=30)
        if self.resources["provenance"]["qemu_revision"] != QEMU_COMMIT:
            raise RuntimeError("image contains a different QEMU revision")
        if args.expected_source and (
            self.resources["provenance"]["source_revision"] != args.expected_source
            or self.identity["labels"].get("org.opencontainers.image.revision")
            != args.expected_source
        ):
            raise RuntimeError("image source provenance does not match the candidate checkout")
        self.cases = []

    def call(self, arguments, **options):
        try:
            return subprocess.run(
                self.docker + arguments,
                check=True,
                text=True,
                capture_output=True,
                timeout=options.pop("timeout", 120),
                **options,
            ).stdout
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f"Docker {arguments[0]} failed: {error.stderr[-4000:]}") from error

    @contextlib.contextmanager
    def container(self, name, command, extra_mounts=()):
        case = self.out / name
        case.mkdir()
        arguments = [
            "create",
            "--name",
            f"flapsim-verify-{uuid.uuid4().hex}",
            "--network",
            "none",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--mount",
            mount(self.args.firmware.resolve(), "/firmware.bin", readonly=True),
            "--mount",
            mount(case, "/out"),
        ]
        for source, target in extra_mounts:
            arguments += ["--mount", mount(source, target, readonly=True)]
        container_id = self.call(arguments + [self.image] + command).strip()
        try:
            yield container_id, case
        finally:
            try:
                with (case / "docker.log").open("w") as log:
                    subprocess.run(
                        self.docker + ["logs", container_id],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=30,
                        check=False,
                    )
            finally:
                self.call(["rm", "--force", container_id], timeout=30)

    def command(self, hardware, scenario, *, wrong_version=False, timeout=None):
        command = [
            "run",
            "--firmware",
            "/firmware.bin",
            "--hardware",
            hardware,
            "--scenario",
            scenario,
            "--out",
            "/out/run",
            "--timing",
            "ci",
        ]
        if scenario != "boot":
            command += [
                "--var",
                "FIRMWARE_VERSION=v9.9.9" if wrong_version else "FIRMWARE_VERSION=v1.4.0",
            ]
            if not wrong_version:
                command += ["--golden-version-exception"]
        if timeout is not None:
            command += ["--timeout", str(timeout)]
        return command

    def check_result(self, name, case, exit_code, expected):
        manifest = json.loads((case / "run/run.json").read_text())
        record = {
            "case": name,
            "exit_code": exit_code,
            "expected_exit": expected,
            "wall_seconds": manifest["wall_seconds"],
            "error": manifest.get("error"),
        }
        self.cases.append(record)
        if exit_code != expected or manifest["exit_code"] != expected:
            raise RuntimeError(f"container {name} failed verification: {record}")
        if manifest["passed"] != (expected == 0) or manifest["source_unchanged"] is not True:
            raise RuntimeError(f"container {name} reported inconsistent success/input integrity")
        if manifest["build"]["commit"] != QEMU_COMMIT:
            raise RuntimeError("container run used a different QEMU revision")
        if expected in {0, 5, 143} and manifest.get("qemu_returncode") != 0:
            raise RuntimeError("initialized native emulator did not shut down cleanly")
        for attempt in manifest["attempts"]:
            if not isinstance(attempt.get("returncode"), int):
                raise RuntimeError("container did not record a reaped emulator")
        for artifact in [
            "serial.log",
            "state.json",
            "activity.json",
            "timeline.jsonl",
            "report.md",
            "junit.xml",
            "replay.html",
        ]:
            if not (case / "run" / artifact).is_file():
                raise RuntimeError(f"container {name} missing {artifact}")
        failures = int(ET.parse(case / "run/junit.xml").getroot().get("failures"))
        if (failures == 0) != (expected == 0):
            raise RuntimeError("container JUnit result does not match expected execution")
        return manifest

    def execute(self, name, board, scenario, *, expected=0, wrong_version=False, timeout=None):
        extra_mounts = []
        scale = self.args.timeout_scale
        if scale != 1 and scenario in self.resources["scenarios"]:
            document = yaml.safe_load(self.resources["scenarios"][scenario])
            document["timeout"] *= scale
            for step in document["steps"]:
                step["timeout"] = step.get("timeout", 30) * scale
            scenario_path = self.out / f"{name}-budget.yaml"
            scenario_path.write_text(yaml.safe_dump(document, sort_keys=False))
            extra_mounts.append((scenario_path, "/scenario.yaml"))
            scenario = "/scenario.yaml"
        command = self.command(board, scenario, wrong_version=wrong_version, timeout=timeout)
        with self.container(name, command, extra_mounts) as (container_id, case):
            completed = subprocess.run(
                self.docker + ["start", "--attach", container_id],
                check=False,
                timeout=(timeout or 300 * scale) + 30,
            )
            return self.check_result(name, case, completed.returncode, expected)

    def stop_case(self):
        with self.container("stop", self.command("esp32-8-single", "boot", timeout=60)) as (
            container_id,
            case,
        ):
            self.call(["start", container_id])
            deadline = time.monotonic() + 30
            while True:
                path = case / "run/run.json"
                if path.exists() and json.loads(path.read_text()).get("status") == "executing":
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("container did not finish startup before stop check")
                time.sleep(0.1)
            self.call(["stop", "--time", "8", container_id], timeout=20)
            status = json.loads(self.call(["inspect", container_id]))[0]["State"]
            if status["Running"] or status["Pid"]:
                raise RuntimeError("stopped container retained a process namespace")
            self.check_result("stop", case, status["ExitCode"], 143)


def verify(args):
    positive_timeout(args.timeout_scale)
    original = checked_firmware(args.firmware)
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError("image verification output must be fresh")
    out.mkdir(parents=True, exist_ok=True)
    result = {
        "passed": False,
        "firmware": GOLDEN,
        "mode": args.mode,
        "timeout_scale": args.timeout_scale,
    }
    verifier = None
    try:
        verifier = ImageVerifier(args, out)
        result.update(image=verifier.identity, resources=verifier.resources)
        if args.mode in {"all", "smoke"}:
            verifier.execute("smoke-8", "esp32-8-single", "release-smoke-8")
            verifier.execute("smoke-16", "esp32-16-dual", "release-smoke-16")
        if args.mode in {"all", "lifecycle"}:
            verifier.execute("timeout", "esp32-8-single", "boot", expected=4, timeout=3)
            verifier.stop_case()
        if args.mode == "reject":
            verifier.execute("wrong-version", "esp32-8-single", "version-check", wrong_version=True)
        elif args.mode == "all":
            verifier.execute(
                "wrong-version", "esp32-8-single", "version-check", wrong_version=True, expected=5
            )
        if hashlib.sha256(args.firmware.read_bytes()).digest() != hashlib.sha256(original).digest():
            raise RuntimeError("mounted source firmware changed")
        result["passed"] = True
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        result["cases"] = verifier.cases if verifier else []
        (out / "verification.json").write_text(json.dumps(result, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--firmware", type=Path, default=ROOT / "firmware" / GOLDEN["asset"])
    parser.add_argument("--docker-host")
    parser.add_argument("--expected-source")
    parser.add_argument("--mode", choices=["all", "smoke", "lifecycle", "reject"], default="all")
    parser.add_argument(
        "--timeout-scale",
        type=float,
        default=1,
        help="explicit local-only wall budget multiplier; CI uses 1",
    )
    args = parser.parse_args()
    try:
        verify(args)
    except Exception as error:
        print(f"Container verification failed: {error}", file=sys.stderr)
        return 1
    print(f"PASS: exact image {args.image}; evidence: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
