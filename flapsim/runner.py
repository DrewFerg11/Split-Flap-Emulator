"""Isolated flash preparation, QEMU startup, deadline and lifecycle ownership."""

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from flapsim.executor import (
    ExecutionFailed,
    InterruptedRun,
    RunContext,
    load_executor,
    positive_timeout,
)
from flapsim.hardware import load_hardware, write_model_config
from flapsim.process import enable_subreaper, stop_group
from flapsim.state import read_settlement

FLASH_SIZE = 4 * 1024 * 1024
QEMU_COMMIT = "e39508f79cb0681bbe15cfcebd40a3f45cdc3c48"
EXIT_INVALID = 2
EXIT_RUNTIME = 3
EXIT_TIMEOUT = 4
EXIT_EXECUTION = 5


def firmware_bytes(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file() or not 0 < path.stat().st_size <= FLASH_SIZE:
        raise ValueError("firmware must be a nonempty regular image no larger than 4 MiB")
    data = path.read_bytes()
    if not 0 < len(data) <= FLASH_SIZE:
        raise ValueError("firmware changed size while reading")
    return path, data


def runtime_info(prefix):
    prefix = Path(prefix).resolve(strict=True)
    executable = prefix / "bin/flapsim-qemu"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError(f"missing executable runtime: {executable}")
    for name in [
        "lib/libqemu-xtensa.so",
        "share/qemu/esp32-v3-rom.bin",
        "share/qemu/esp32-v3-rom-app.bin",
    ]:
        if not (prefix / name).is_file() or (prefix / name).stat().st_size == 0:
            raise ValueError(f"missing runtime dependency: {prefix / name}")
    try:
        info = json.loads((prefix / "build-info.json").read_text())
        if (
            not isinstance(info, dict)
            or info.get("commit") != QEMU_COMMIT
            or not re.fullmatch(r"[0-9a-f]{64}", str(info.get("integration_sha256", "")))
        ):
            raise ValueError("runtime provenance does not match the pinned QEMU integration")
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read runtime provenance: {error}") from error
    return prefix, info


def claim_output(path):
    out = Path(path).resolve()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError("--out must be a fresh or empty directory")
    out.mkdir(parents=True, exist_ok=True)
    try:
        with (out / ".run-owner").open("x") as marker:
            marker.write(f"{os.getpid()}\n")
    except FileExistsError as error:
        raise ValueError("output directory is already owned by another run") from error
    return out


def choose_port():
    # This is a candidate only: QEMU's actual bind is checked, with bounded retries.
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return candidate.getsockname()[1]


def command_for(prefix, out, port, qmp, timing):
    flash = str(out / "flash.bin").replace(",", ",,")
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
        "-qmp",
        f"unix:{qmp},server=on,wait=off",
        "-nic",
        f"user,model=esp32_wifi,net=192.168.4.0/24,hostfwd=tcp:127.0.0.1:{port}-192.168.4.1:80",
        "-global",
        "driver=timer.esp32.timg,property=wdt_disable,value=true",
    ]
    if timing in {"ci", "accurate"}:
        command += ["-icount", f"shift={3 if timing == 'ci' else 2},sleep=off"]
    elif timing != "realtime":
        raise ValueError("timing must be ci, accurate or realtime")
    return command


def qmp_ready(path, context):
    """A successful query-status response proves qemu_init reached its main loop."""
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(min(0.1, max(0.001, context.deadline - time.monotonic())))
            connection.connect(str(path))
            connection.sendall(
                b'{"execute":"qmp_capabilities","id":"cap"}\n'
                b'{"execute":"query-status","id":"status"}\n'
            )
            data = b""
            while True:
                context.checkpoint()
                try:
                    chunk = connection.recv(65536)
                except TimeoutError:
                    # Retain partial replies and this connection under CPU contention.
                    continue
                if not chunk:
                    return False
                data += chunk
                while b"\n" in data:
                    line, data = data.split(b"\n", 1)
                    message = json.loads(line)
                    if message.get("id") == "status":
                        if message.get("return", {}).get("status") != "running":
                            raise RuntimeError(f"QEMU did not enter running state: {message}")
                        return True
                if len(data) > 1024 * 1024:
                    raise RuntimeError("oversized QMP startup message")
    except (TimeoutError, FileNotFoundError, ConnectionRefusedError, ConnectionResetError):
        return False


def save_manifest(out, manifest):
    temporary = out / "run.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    temporary.replace(out / "run.json")


def run(args, *, executor=None, interrupt=None):
    """Return an exit code. Executors cooperate via context.checkpoint/pause for deadlines."""
    started = time.monotonic()
    # All user configuration/runtime dependencies are checked before creating a child.
    source, data = firmware_bytes(args.firmware)
    digest = hashlib.sha256(data).hexdigest()
    hardware = load_hardware(args.hardware, seed=args.seed)
    executor = executor or load_executor(
        args.scenario,
        hardware,
        variables=getattr(args, "variables", ()),
        firmware_sha256=digest,
        golden_exception=getattr(args, "golden_version_exception", False),
    )
    timeout = positive_timeout(args.timeout if args.timeout is not None else executor.timeout)
    prefix, build = runtime_info(args.qemu_prefix)
    if interrupt and (signum := interrupt()) is not None:
        raise InterruptedRun(signum)
    if args.timing not in {"ci", "accurate", "realtime"}:
        raise ValueError("timing must be ci, accurate or realtime")
    enable_subreaper()
    out = claim_output(args.out)
    manifest = {
        "schema_version": 1,
        "passed": False,
        "status": "preparing",
        "firmware": {"path": str(source), "size": len(data), "sha256": digest},
        "hardware": hardware,
        "seed": hardware["seed"],
        "build": build,
        "timing": args.timing,
        "scenario": executor.document,
        "timeout_seconds": timeout,
        "attempts": [],
    }
    process = None
    exit_code = EXIT_RUNTIME
    try:
        (out / "flash.bin").write_bytes(data + b"\xff" * (FLASH_SIZE - len(data)))
        config = write_model_config(hardware, out, trace=args.trace)
        deadline = started + timeout
        with tempfile.TemporaryDirectory(prefix="flapsim-") as control:
            for attempt_number in range(1, 6):
                port = choose_port()
                qmp = Path(control) / f"qmp-{attempt_number}.sock"
                command = command_for(prefix, out, port, qmp, args.timing)
                manifest.update(command=command, endpoint=f"http://127.0.0.1:{port}", port=port)
                attempt = {"number": attempt_number, "port": port, "command": command}
                manifest["attempts"].append(attempt)
                manifest["status"] = "starting"
                save_manifest(out, manifest)
                # A failed startup never consumes a partially mutated flash in the next attempt.
                (out / "flash.bin").write_bytes(data + b"\xff" * (FLASH_SIZE - len(data)))
                for name in [
                    "state.json",
                    "activity.json",
                    "timeline.jsonl",
                    "serial.log",
                    "i2c.log",
                ]:
                    (out / name).unlink(missing_ok=True)
                with (out / f"qemu-attempt-{attempt_number}.log").open("wb") as log:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("deadline reached before QEMU launch")
                    process = subprocess.Popen(
                        command,
                        env={**os.environ, "FLAPSIM_CONFIG": str(config)},
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    attempt["pid"] = process.pid
                    context = RunContext(
                        out, hardware, manifest["endpoint"], process, deadline, interrupt
                    )
                    try:
                        while True:
                            context.checkpoint()
                            if qmp_ready(qmp, context):
                                break
                            context.pause()
                        manifest["status"] = "executing"
                        save_manifest(out, manifest)
                        manifest["result"] = executor.execute(context)
                        context.checkpoint()
                        break
                    except RuntimeError:
                        if (
                            manifest["status"] == "starting"
                            and process.poll() is not None
                            and "Could not set up host forwarding rule"
                            in (out / f"qemu-attempt-{attempt_number}.log").read_text(
                                errors="replace"
                            )
                            and attempt_number < 5
                        ):
                            attempt["retry"] = "HTTP forwarding bind failed"
                            continue
                        raise
                    finally:
                        try:
                            stop_group(process)
                        finally:
                            attempt["returncode"] = process.poll()
                            manifest["qemu_returncode"] = process.poll()
                        process = None
        # Confirm complete artifacts after stopping, not merely a transient success flag.
        if not (out / "serial.log").is_file() or not (out / "timeline.jsonl").is_file():
            raise RuntimeError("missing serial/model diagnostics")
        if read_settlement(out, hardware) is None:
            raise RuntimeError("missing fresh final model output")
        validate_timeline(out / "timeline.jsonl")
        if hasattr(executor, "verify_final"):
            executor.verify_final(out, hardware)
        manifest["status"] = "passed"
        exit_code = 0
    except InterruptedRun as error:
        exit_code = 128 + error.signum
        manifest["error"] = str(error)
    except KeyboardInterrupt:
        exit_code = 130
        manifest["error"] = "interrupted by SIGINT"
    except TimeoutError as error:
        exit_code = EXIT_TIMEOUT
        manifest["error"] = str(error)
    except ExecutionFailed as error:
        exit_code = EXIT_EXECUTION
        manifest["error"] = str(error)
    except Exception as error:
        manifest["error"] = str(error) or type(error).__name__
    finally:
        if process is not None:
            try:
                stop_group(process)
            except Exception as error:
                manifest["cleanup_error"] = str(error)
                exit_code = EXIT_RUNTIME
        try:
            manifest["source_unchanged"] = hashlib.sha256(source.read_bytes()).hexdigest() == digest
            if not manifest["source_unchanged"]:
                raise RuntimeError("source firmware changed during execution")
        except Exception as error:
            manifest["error"] = str(error)
            exit_code = EXIT_RUNTIME
        if interrupt and (signum := interrupt()) is not None:
            manifest["error"] = str(InterruptedRun(signum))
            exit_code = 128 + signum
        manifest["passed"] = exit_code == 0
        if exit_code:
            manifest["status"] = "failed"
        manifest["exit_code"] = exit_code
        manifest["wall_seconds"] = round(time.monotonic() - started, 3)
        try:
            if hasattr(executor, "finalize"):
                executor.finalize(out, manifest)
            else:
                from flapsim.report import write_reports

                write_reports(out, manifest, [], None)
        except Exception as error:
            manifest.update(
                passed=False, status="failed", exit_code=EXIT_RUNTIME, report_error=str(error)
            )
            exit_code = EXIT_RUNTIME
        try:
            save_manifest(out, manifest)
        except OSError as error:
            print(f"Cannot finalize run manifest: {error}", file=sys.stderr)
            exit_code = EXIT_RUNTIME
    return exit_code


def validate_timeline(path):
    sequence = -1
    with path.open() as timeline:
        for line in timeline:
            value = json.loads(line)
            if (
                not line.endswith("\n")
                or not isinstance(value, dict)
                or type(value.get("schema_version")) is not int
                or value["schema_version"] != 1
                or type(value.get("sequence")) is not int
                or value["sequence"] <= sequence
                or not isinstance(value.get("event"), str)
            ):
                raise RuntimeError("incomplete or invalid model timeline")
            sequence = value["sequence"]
    if sequence < 0:
        raise RuntimeError("empty model timeline")
