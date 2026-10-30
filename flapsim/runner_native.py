"""Native mode: the firmware source build runs as a host process against the shims."""

import json
import os
import subprocess
import time

from flapsim import mqtt, native
from flapsim.baseline import load_known_issues
from flapsim.control import channel
from flapsim.executor import (
    InterruptedRun,
    RunContext,
    load_executor,
    positive_timeout,
)
from flapsim.hardware import load_hardware
from flapsim.process import enable_subreaper, start_time, stop_group
from flapsim.provenance import emulator_provenance
from flapsim.runner import (
    EXIT_RUNTIME,
    choose_port,
    claim_output,
    failure_code,
    finalize,
    save_manifest,
    verify_outputs,
)


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
        if control_port is not None or getattr(executor, "requires_control", False):
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
                    control,
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
