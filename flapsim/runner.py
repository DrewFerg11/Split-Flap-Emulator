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

from flapsim import mqtt, native
from flapsim.baseline import load_known_issues
from flapsim.control import channel
from flapsim.executor import (
    ExecutionFailed,
    InterruptedRun,
    RunContext,
    load_executor,
    positive_timeout,
)
from flapsim.hardware import load_hardware, write_model_config
from flapsim.process import enable_subreaper, start_time, stop_group
from flapsim.provenance import emulator_provenance
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
    if getattr(args, "mode", "qemu") == "native":
        return run_native(args, executor=executor, interrupt=interrupt)
    started = time.monotonic()
    # All user configuration/runtime dependencies are checked before creating a child.
    source, data = firmware_bytes(args.firmware)
    digest = hashlib.sha256(data).hexdigest()
    hardware = load_hardware(args.hardware, seed=args.seed)
    known_issues = None
    if getattr(args, "known_issues", None) is not None:
        known_issues = load_known_issues(args.known_issues)
        known_issues.check_firmware(digest)
    executor = executor or load_executor(
        args.scenario,
        hardware,
        variables=getattr(args, "variables", ()),
        firmware_sha256=digest,
        known_issues=known_issues,
    )
    timeout = positive_timeout(args.timeout if args.timeout is not None else executor.timeout)
    prefix, build = runtime_info(args.qemu_prefix)
    emulator = emulator_provenance()
    if interrupt and (signum := interrupt()) is not None:
        raise InterruptedRun(signum)
    if args.timing not in {"ci", "accurate", "realtime"}:
        raise ValueError("timing must be ci, accurate or realtime")
    control_port = getattr(args, "control_port", None)
    if getattr(args, "i2c_history", False) and control_port is None:
        raise ValueError("--i2c-history requires --control-port")
    if control_port is not None:
        from flapsim.control import check_port

        check_port(control_port, telemetry=getattr(args, "i2c_history", False))
    enable_subreaper()
    out = claim_output(args.out)
    manifest = {
        "schema_version": 1,
        "mode": "qemu",
        "passed": False,
        "status": "preparing",
        "firmware": {"path": str(source), "size": len(data), "sha256": digest},
        "hardware": hardware,
        "seed": hardware["seed"],
        "build": build,
        "emulator": emulator,
        "timing": args.timing,
        "scenario": executor.document,
        "timeout_seconds": timeout,
        "attempts": [],
    }
    process = None
    control = None
    exit_code = EXIT_RUNTIME
    try:
        if control_port is not None:
            from flapsim.control import ControlServer

            control = ControlServer(
                control_port,
                out,
                hardware,
                timing=args.timing,
                telemetry=getattr(args, "i2c_history", False),
            )
            manifest["control"] = control.metadata()
        (out / "flash.bin").write_bytes(data + b"\xff" * (FLASH_SIZE - len(data)))
        config = write_model_config(hardware, out, trace=args.trace)
        deadline = started + timeout
        with tempfile.TemporaryDirectory(prefix="flapsim-") as control_dir:
            for attempt_number in range(1, 6):
                port = choose_port()
                qmp = Path(control_dir) / f"qmp-{attempt_number}.sock"
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
                with (
                    channel(control) as (control_env, pass_fds, connection, child_channel),
                    (out / f"qemu-attempt-{attempt_number}.log").open("wb") as log,
                ):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("deadline reached before QEMU launch")
                    process = subprocess.Popen(
                        command,
                        env={
                            **{
                                k: v
                                for k, v in os.environ.items()
                                if k not in {"FLAPSIM_CONTROL_FD", "FLAPSIM_I2C_FD"}
                            },
                            "FLAPSIM_CONFIG": str(config),
                            **control_env,
                        },
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        pass_fds=pass_fds,
                    )
                    if child_channel is not None:
                        child_channel.close()
                    attempt["pid"] = process.pid
                    attempt["start_time"] = start_time(process.pid)
                    context = RunContext(
                        out, hardware, manifest["endpoint"], process, deadline, interrupt
                    )
                    try:
                        while True:
                            context.checkpoint()
                            if qmp_ready(qmp, context):
                                break
                            context.pause()
                        if control is not None:
                            from flapsim.lifecycle import qmp_reset
                            from flapsim.power import QemuPower

                            control.attach(
                                connection,
                                reset=lambda path=qmp: qmp_reset(path),
                                power=QemuPower(qmp),
                            )
                            manifest["control"] = control.metadata()
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
                            if control is not None and manifest["status"] == "executing":
                                control.detach()
                                manifest["control"] = {**control.metadata(), "status": "stopped"}
                            stop_group(process)
                        finally:
                            attempt["returncode"] = process.poll()
                            manifest["qemu_returncode"] = process.poll()
                        process = None
        exit_code = verify_outputs(out, hardware, executor, manifest)
    except BaseException as error:
        exit_code = failure_code(error, manifest)
    finally:
        if process is not None:
            try:
                stop_group(process)
            except Exception as error:
                manifest["cleanup_error"] = str(error)
                exit_code = EXIT_RUNTIME
        if control is not None:
            try:
                control.close()
                manifest["control"] = {**control.metadata(), "status": "stopped"}
            except Exception as error:
                manifest["cleanup_error"] = str(error)
                exit_code = EXIT_RUNTIME
        exit_code = finalize(
            out, manifest, executor, exit_code, started, interrupt, source, digest, "firmware"
        )
    return exit_code


def run_native(args, *, executor=None, interrupt=None):
    """Native mode: the firmware source build runs as a process against the host shims."""
    started = time.monotonic()
    if args.native_build is None:
        # Built inside the fresh output directory, so its log and inputs are part of the evidence.
        out = claim_output(args.out)
        args.native_build = native.build_from_source(
            args.firmware_src, out / "native-build", out / "native-build.log"
        )
        started = time.monotonic()
    build = native.load_build(args.native_build)
    firmware = build["info"]["firmware"]
    speed = native.check_speed(args.speed if args.speed is not None else native.DEFAULT_SPEED)
    hardware = load_hardware(args.hardware, seed=args.seed)
    known_issues = None
    if getattr(args, "known_issues", None) is not None:
        known_issues = load_known_issues(args.known_issues)
        known_issues.check_source(firmware.get("commit"), firmware.get("dirty"))
    broker_port = choose_port()
    executor = executor or load_executor(
        args.scenario,
        hardware,
        variables=getattr(args, "variables", ()),
        firmware_source=firmware,
        known_issues=known_issues,
        mode="native",
        implicit_variables={"MQTT_HOST": "127.0.0.1", "MQTT_PORT": broker_port},
    )
    setup = getattr(executor, "native_setup", None) or {"services": [], "nvs": {}, "clock": None}
    timeout = positive_timeout(args.timeout if args.timeout is not None else executor.timeout)
    emulator = emulator_provenance()
    if interrupt and (signum := interrupt()) is not None:
        raise InterruptedRun(signum)
    control_port = getattr(args, "control_port", None)
    if getattr(args, "i2c_history", False) and control_port is None:
        raise ValueError("--i2c-history requires --control-port")
    if control_port is not None:
        from flapsim.control import check_port

        check_port(control_port, telemetry=getattr(args, "i2c_history", False))
    enable_subreaper()
    if args.firmware_src is None:
        out = claim_output(args.out)
    manifest = {
        "schema_version": 1,
        "mode": "native",
        "passed": False,
        "status": "preparing",
        "firmware": {
            **firmware,
            "binary": {
                "path": str(build["binary"]),
                "size": build["binary_size"],
                "sha256": build["binary_sha256"],
            },
        },
        "hardware": hardware,
        "seed": hardware["seed"],
        "native": {
            **build["info"],
            "build_dir": str(build["path"]),
            "speed": speed,
            "setup": setup,
        },
        "emulator": emulator,
        "scenario": executor.document,
        "timeout_seconds": timeout,
        "attempts": [],
    }
    process = None
    owner = None
    control = None
    broker = observer = None
    exit_code = EXIT_RUNTIME
    try:
        if control_port is not None:
            from flapsim.control import ControlServer

            control = ControlServer(
                control_port,
                out,
                hardware,
                backend="native",
                speed=speed,
                telemetry=getattr(args, "i2c_history", False),
            )
            manifest["control"] = control.metadata()
        deadline = started + timeout
        services = {}
        if "mqtt" in setup["services"]:
            broker = mqtt.Broker(out, broker_port)
            manifest["mqtt"] = {"port": broker_port, "command": broker.command}
            broker.wait_ready(RunContext(out, hardware, "", broker.process, deadline, interrupt))
            observer = services["mqtt"] = mqtt.Observer(broker_port)
        with native.listener() as server:
            port = server.getsockname()[1]
            config = native.prepare(
                out,
                build,
                hardware,
                trace=args.trace,
                speed=speed,
                http_fd=server.fileno(),
                seed=hardware["seed"],
                setup=setup,
            )
            command = [str(build["binary"]), "--config", str(config)]
            manifest.update(command=command, endpoint=f"http://127.0.0.1:{port}", port=port)
            attempt = {"number": 1, "port": port, "command": command}
            manifest["attempts"].append(attempt)
            manifest["status"] = "starting"
            save_manifest(out, manifest)
            with (
                channel(control) as (control_env, pass_fds, connection, child_channel),
                (out / "native-attempt-1.log").open("wb") as log,
            ):
                if time.monotonic() >= deadline:
                    raise TimeoutError("deadline reached before native launch")
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env={
                        **{
                            k: v
                            for k, v in os.environ.items()
                            if k not in {"FLAPSIM_CONTROL_FD", "FLAPSIM_I2C_FD"}
                        },
                        **control_env,
                    },
                    pass_fds=(server.fileno(), *pass_fds),
                )
                if child_channel is not None:
                    child_channel.close()
                attempt["pid"] = process.pid
                attempt["start_time"] = start_time(process.pid)
                if control is not None:
                    from flapsim.power import NativePower

                    def launch_power_on(child, resume):
                        if time.monotonic() >= deadline:
                            raise TimeoutError("deadline reached before power-on")
                        if interrupt and interrupt() is not None:
                            raise TimeoutError("interrupted before power-on")
                        return subprocess.Popen(
                            command,
                            stdin=subprocess.DEVNULL,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            start_new_session=True,
                            env={
                                **{
                                    k: v
                                    for k, v in os.environ.items()
                                    if k
                                    not in {
                                        "FLAPSIM_CONTROL_FD",
                                        "FLAPSIM_I2C_FD",
                                        "FLAPSIM_NATIVE_BOOT",
                                    }
                                },
                                **{
                                    k: v
                                    for k, v in control_env.items()
                                    if k != "FLAPSIM_CONTROL_FD"
                                },
                                "FLAPSIM_CONTROL_FD": str(child.fileno()),
                                "FLAPSIM_NATIVE_BOOT": resume,
                            },
                            pass_fds=(server.fileno(), child.fileno(), *pass_fds[1:]),
                        )

                    owner = NativePower(process, launch_power_on)
                context = RunContext(
                    out,
                    hardware,
                    manifest["endpoint"],
                    owner or process,
                    deadline,
                    interrupt,
                    services,
                )
                try:
                    while not native.ready(out):
                        context.pause()
                    if control is not None:
                        control.attach(connection, power=owner)
                        manifest["control"] = control.metadata()
                    manifest["status"] = "executing"
                    save_manifest(out, manifest)
                    manifest["result"] = executor.execute(context)
                    context.checkpoint()
                finally:
                    try:
                        if control is not None:
                            control.detach()
                            manifest["control"] = {**control.metadata(), "status": "stopped"}
                        owner.stop() if owner is not None else stop_group(process)
                    finally:
                        attempt["returncode"] = (owner.current if owner else process).poll()
                        manifest["native_returncode"] = attempt["returncode"]
                        if owner is not None:
                            attempt["power_processes"] = owner.history
                    process = None
        exit_code = verify_outputs(out, hardware, executor, manifest)
    except BaseException as error:
        exit_code = failure_code(error, manifest)
    finally:
        if process is not None:
            try:
                owner.stop() if owner is not None else stop_group(process)
            except Exception as error:
                manifest["cleanup_error"] = str(error)
                exit_code = EXIT_RUNTIME
        if control is not None:
            try:
                control.close()
                manifest["control"] = {**control.metadata(), "status": "stopped"}
            except Exception as error:
                manifest["cleanup_error"] = str(error)
                exit_code = EXIT_RUNTIME
        if observer is not None:
            observer.close()
            messages = observer.snapshot()
            with (out / "mqtt.jsonl").open("w") as evidence:
                for message in messages:
                    record = {k: v for k, v in message.items() if k != "monotonic"}
                    evidence.write(json.dumps(record) + "\n")
            manifest["mqtt"]["messages"] = len(messages)
        if broker is not None:
            try:
                manifest["mqtt"]["returncode"] = broker.stop()
            except Exception as error:
                manifest["cleanup_error"] = str(error)
                exit_code = EXIT_RUNTIME
        exit_code = finalize(
            out,
            manifest,
            executor,
            exit_code,
            started,
            interrupt,
            build["binary"],
            build["binary_sha256"],
            "native executable",
        )
    return exit_code


def verify_outputs(out, hardware, executor, manifest):
    """Confirm complete artifacts after stopping, not merely a transient success flag."""
    if not (out / "serial.log").is_file() or not (out / "timeline.jsonl").is_file():
        raise RuntimeError("missing serial/model diagnostics")
    power = manifest.get("control", {}).get("power", {})
    if power.get("state") == "off":
        from flapsim.state import validate_state

        validate_state(power["snapshot"], hardware, require_settled=False)
    elif read_settlement(out, hardware) is None:
        raise RuntimeError("missing fresh final model output")
    validate_timeline(out / "timeline.jsonl")
    if hasattr(executor, "verify_final"):
        executor.verify_final(out, hardware)
    summary = executor.known_issue_summary() if hasattr(executor, "known_issue_summary") else None
    manifest["status"] = "passed_with_known_issues" if summary and summary["applied"] else "passed"
    return 0


def failure_code(error, manifest):
    """Map a run failure to its documented exit code; anything unexpected is a runtime error."""
    if isinstance(error, InterruptedRun):
        manifest["error"] = str(error)
        return 128 + error.signum
    if isinstance(error, KeyboardInterrupt):
        manifest["error"] = "interrupted by SIGINT"
        return 130
    if not isinstance(error, Exception):
        raise error
    manifest["error"] = str(error) or type(error).__name__
    if isinstance(error, TimeoutError):
        return EXIT_TIMEOUT
    if isinstance(error, ExecutionFailed):
        return EXIT_EXECUTION
    return EXIT_RUNTIME


def finalize(out, manifest, executor, exit_code, started, interrupt, source, digest, what):
    """Shared closing evidence: input integrity, verdict, reports and the atomic manifest."""
    try:
        manifest["source_unchanged"] = hashlib.sha256(source.read_bytes()).hexdigest() == digest
        if not manifest["source_unchanged"]:
            raise RuntimeError(f"source {what} changed during execution")
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

            write_reports(out, manifest, [])
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
