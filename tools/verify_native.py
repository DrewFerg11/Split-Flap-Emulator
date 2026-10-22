#!/usr/bin/env python3
"""Bounded native-mode verification of a built firmware: boot and an Improv-driven reboot.

`boot` requires the unmodified firmware to home and settle every module at X with only the
fresh-flash diagnostics QEMU mode also allows. `improv-reboot` additionally provisions Wi-Fi
over the serial pipe, then requires an emulated software reset (same process, re-executed),
persisted NVS credentials, a continuous model timeline whose drums resume exactly where they
stopped, and the connected boot path homing to OK.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flapsim.hardware import load_hardware, write_model_config  # noqa: E402
from flapsim.process import stop_group  # noqa: E402
from flapsim.runner import validate_timeline  # noqa: E402
from flapsim.scenario import serial_errors  # noqa: E402
from flapsim.state import read_settlement, validate_state  # noqa: E402

CASES = ["boot", "improv-reboot", "control"]


class KnownSerial:
    """Firmware diagnostics native mode reproduces from the golden baseline's linked issues."""

    ENTRIES = [
        {
            "issue": "DrewFerg11/Split-Flap-Display#54",
            "reason": "item 4: with the namespace present, unwritten keys log NOT_FOUND",
            "regex": r"^\[\s*\d+\]\[E\]\[Preferences\.cpp:\d+\] (getString|getBytesLength)\(\): "
            r"nvs_get_(str|blob) len fail: \w+ NOT_FOUND$",
        }
    ]

    def __init__(self):
        self.compiled = [(e, re.compile(e["regex"])) for e in self.ENTRIES]

    def tolerated_serial(self, line):
        return next((e for e, pattern in self.compiled if pattern.fullmatch(line)), None)


def improv_packet(ssid, password):
    """An Improv serial RPC frame carrying WIFI_SETTINGS."""
    settings = bytes([len(ssid)]) + ssid.encode() + bytes([len(password)]) + password.encode()
    data = bytes([0x01, len(settings)]) + settings
    frame = b"IMPROV" + bytes([1, 0x03, len(data)]) + data
    return frame + bytes([sum(frame) & 0xFF])


def records(out):
    """Complete timeline records; a final line still being written is left for the next poll."""
    with (out / "timeline.jsonl").open() as timeline:
        return [json.loads(line) for line in timeline if line.endswith("\n")]


class Run:
    def __init__(self, build, hardware_name, out, speed, serial_input, control=False):
        self.out = out.resolve()
        if self.out.exists() and any(self.out.iterdir()):
            raise ValueError(f"{self.out} must be a fresh or empty directory")
        self.out.mkdir(parents=True, exist_ok=True)
        self.hardware = load_hardware(hardware_name)
        littlefs = self.out / "littlefs"
        subprocess.run(["cp", "-r", str(build / "littlefs"), str(littlefs)], check=True)
        config = {
            "schema_version": 1,
            "model_config": str(write_model_config(self.hardware, self.out)),
            "serial_log": str(self.out / "serial.log"),
            "nvs": str(self.out / "nvs.json"),
            "littlefs": str(littlefs),
            "speed": speed,
        }
        if serial_input:
            config["serial_input"] = str(self.out / "serial.in")
            os.mkfifo(self.out / "serial.in")
        (self.out / "native.json").write_text(json.dumps(config, indent=2) + "\n")
        self.command = [
            str(build / "bin/flapsim-native"),
            "--config",
            str(self.out / "native.json"),
        ]
        self.log = (self.out / "native.log").open("wb")
        self.resources = ExitStack()
        self.control = None
        control_env, pass_fds, connection, child_channel = {}, (), None, None
        try:
            if control:
                from flapsim.control import ControlServer, channel

                self.control = ControlServer(0, self.out, self.hardware, backend="native")
                self.resources.callback(self.control.close)
                control_env, pass_fds, connection, child_channel = self.resources.enter_context(
                    channel(self.control)
                )
            self.process = subprocess.Popen(
                self.command,
                env={
                    **{k: v for k, v in os.environ.items() if k != "FLAPSIM_CONTROL_FD"},
                    **control_env,
                },
                pass_fds=pass_fds,
                stdin=subprocess.DEVNULL,
                stdout=self.log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            if child_channel is not None:
                child_channel.close()
                self.control.attach(connection)
        except BaseException:
            if hasattr(self, "process"):
                stop_group(self.process)
            self.resources.close()
            self.log.close()
            raise

    def api(self, path="/state", body=None):
        request = urllib.request.Request(
            self.control.endpoint + "/api/emu" + path,
            headers={
                "Authorization": "Bearer " + self.control.token,
                "Content-Type": "application/json",
            },
            data=None if body is None else json.dumps(body).encode(),
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=7) as response:
            return json.load(response)

    def serial(self):
        try:
            return (self.out / "serial.log").read_text(errors="replace")
        except FileNotFoundError:
            return ""

    def wait(self, description, condition, deadline):
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"native process exited early ({self.process.returncode})")
            value = condition()
            if value:
                return value
            time.sleep(0.05)
        raise TimeoutError(f"timed out waiting for {description}")

    def settled(self, text):
        def check():
            state = read_settlement(self.out, self.hardware)
            return state if state and state["movement_id"] > 0 and state["text"] == text else None

        return check

    def stop(self):
        try:
            return stop_group(self.process)
        finally:
            self.log.close()
            self.resources.close()


def check_faults(state):
    if state["counters"]["stalls"] or state["counters"]["anomalies"]:
        raise RuntimeError(f"model faults: {state['counters']}")


def verify(args):
    build = args.build.resolve()
    info = json.loads((build / "build-info.json").read_text())
    run = Run(
        build,
        args.hardware,
        args.out,
        args.speed,
        args.case in {"improv-reboot", "control"},
        control=args.case == "control",
    )
    hardware = run.hardware
    count = len(hardware["modules"])
    default_eight = [m["addr"] for m in hardware["modules"]] == list(range(32, 40)) and all(
        m["bus"] == 0 for m in hardware["modules"]
    )
    policy = {"fresh_nvs": True, "default_missing_bus1": default_eight}
    result = {"case": args.case, "hardware": args.hardware, "speed": args.speed, "build": info}
    started = time.monotonic()
    deadline = started + args.timeout
    try:
        if args.case == "control":

            def moving():
                snapshot = run.api()
                return snapshot if not snapshot["state"]["settled"] else None

            result["control"] = {"moving": run.wait("live moving snapshot", moving, deadline)}
        boot = run.wait("startup settlement at X", run.settled("X" * count), deadline)
        check_faults(boot)
        serial = run.serial()
        if f"Firmware: {info['firmware']['version']} (flapsim native)" not in serial:
            raise RuntimeError("serial banner does not report the built firmware version")
        if "Reset reason: power-on" not in serial:
            raise RuntimeError("first boot did not report a power-on reset")
        serial_errors(serial[: serial.rfind("\n") + 1], policy)
        result["boot"] = {"guest_ns": boot["guest_ns"], "text": boot["text"]}
        if args.case == "control":
            snapshot = run.api()
            if snapshot["backend"] != "native" or snapshot["boot_generation"] != 0:
                raise RuntimeError("incorrect native snapshot identity")
            before_edit = snapshot["state"]["modules"][0]
            edit = {
                "id": 1,
                "expected_revision": 0,
                "patch": {"sensor_window": before_edit["mechanics"]["sensor_window"] + 1},
            }
            applied = run.api("/modules/0/32/mech", edit)
            if applied["status"] != "applied":
                raise RuntimeError("native mechanics edit was not applied")
            after_edit = run.api()["state"]["modules"][0]
            if before_edit["angle"] != after_edit["angle"]:
                raise RuntimeError("mechanics edit moved a drum")
            result["control"].update(initial=snapshot, applied=applied)
        if args.case in {"improv-reboot", "control"}:
            pid = run.process.pid
            with (run.out / "serial.in").open("wb") as pipe:
                pipe.write(improv_packet("flapsim-net", "secret-pass"))
            run.wait(
                "software reset", lambda: "Reset reason: software reset" in run.serial(), deadline
            )
            for expected in [
                'Improv: connected to Wi-Fi network "flapsim-net"',
                "flapsim native boot 2",
                "Connected to Wi-Fi!",
                "IP Address: http://127.0.0.1",
            ]:
                run.wait(expected, lambda e=expected: e in run.serial(), deadline)
            if run.process.pid != pid or run.process.poll() is not None:
                raise RuntimeError("the emulated reset did not keep the supervised process")
            nvs = json.loads((run.out / "nvs.json").read_text())["namespaces"]["config"]
            if nvs.get("ssid", {}).get("value") != "flapsim-net":
                raise RuntimeError("Improv credentials were not persisted in NVS")

            def connected_ok():
                items = records(run.out)
                reset = [i for i, r in enumerate(items) if r["event"] == "guest_reset"]
                if len(reset) > 1:
                    # e.g. a retried Improv frame provisioning twice: report it, not a timeout.
                    raise RuntimeError(f"expected one emulated reset, found {len(reset)}")
                if not reset:
                    return None
                after = items[reset[0] :]
                ok = [
                    r for r in after if r["event"] == "move_finished" and r["text"].strip() == "OK"
                ]
                blank = [
                    r
                    for r in after
                    if r["event"] == "move_finished"
                    and r["text"] == " " * count
                    and ok
                    and r["sequence"] > ok[0]["sequence"]
                ]
                return (items, reset[0], ok[0], blank[0]) if ok and blank else None

            items, index, ok, blank = run.wait(
                "connected OK homing then blank", connected_ok, deadline
            )
            before, reset = items[index - 1], items[index]
            for a, b in zip(before["modules"], reset["modules"], strict=True):
                if (a["angle"], a["latch"], a["phase"]) != (b["angle"], b["latch"], b["phase"]):
                    raise RuntimeError("drums did not resume where the reset left them")
            if reset["guest_ns"] < before["guest_ns"]:
                raise RuntimeError("guest time moved backwards across the reset")
            validate_state(ok, hardware)
            check_faults(blank)
            result["reboot"] = {
                "reset_guest_ns": reset["guest_ns"],
                "ok_text": ok["text"],
                "final_text": blank["text"],
            }
        if args.case == "control":
            after_reset = run.api()
            if after_reset["boot_generation"] != 1:
                raise RuntimeError("native reset generation was not retained")
            receipt = run.api("/operations/1")
            duplicate = run.api("/modules/0/32/mech", edit)
            if (
                receipt["operation"] != applied["operation"]
                or duplicate["operation"] != applied["operation"]
            ):
                raise RuntimeError("native reset lost or repeated the operation")
            if (
                after_reset["state"]["modules"][0]["mechanics"]["sensor_window"]
                != edit["patch"]["sensor_window"]
            ):
                raise RuntimeError("native reset lost the mechanics overlay")
            result["control"].update(after_reset=after_reset, receipt=receipt, duplicate=duplicate)
            # Controller resets use the public API, retain NVS/overlay and
            # are observed before success. Retry the first ID, then reset during motion.
            first = run.api("/reset", {"id": 1})
            if first["status"] != "applied" or first["boot_generation"] != 2:
                raise RuntimeError("public native reset was not observed")
            if run.api("/reset", {"id": 1}) != first:
                raise RuntimeError("public native reset was repeated")
            run.wait("motion after public reset", moving, deadline)
            second = run.api("/reset", {"id": 2})
            if second["status"] != "applied" or second["boot_generation"] != 3:
                raise RuntimeError("native reset during motion was not observed")

            def final_blank():
                items = records(run.out)
                ok = [
                    r
                    for r in items
                    if r["sequence"] > second["sequence"]
                    and r["event"] == "move_finished"
                    and r["text"].strip() == "OK"
                ]
                if not ok:
                    return None
                value = run.api()
                state = value["state"]
                return (
                    state
                    if (
                        value["boot_generation"] == 3
                        and state["settled"]
                        and state["sequence"] > ok[0]["sequence"]
                        and state["text"] == " " * count
                    )
                    else None
                )

            check_faults(run.wait("post-reset blank settlement", final_blank, deadline))
            if run.process.pid != pid or run.process.poll() is not None:
                raise RuntimeError("public reset lost the supervised native PID")
            if run.api("/operations/1")["operation"] != applied["operation"]:
                raise RuntimeError("public reset lost the mechanics receipt")
            result["control"].update(public_reset=first, moving_reset=second)
            items = records(run.out)
            resets = [i for i, record in enumerate(items) if record["event"] == "guest_reset"]
            if len(resets) != 3:
                raise RuntimeError("public reset was applied more than once")
            # Idle resets have an exact adjacent boundary. During motion the
            # previous sampled timeline frame can lag the actual reset; exact
            # moving-state restore is covered by the C model resume regression.
            for index in resets[:-1]:
                if items[index]["modules"] != items[index - 1]["modules"]:
                    raise RuntimeError("native reset changed external mechanics")
            if run.serial().count("Reset reason: software reset") != 3:
                raise RuntimeError("native public reset reported the wrong reset reason")
        returncode = run.stop()
        validate_timeline(run.out / "timeline.jsonl")
        final = read_settlement(run.out, hardware)
        if final is None:
            raise RuntimeError("missing final settlement")
        check_faults(final)
        serial = run.serial()
        observed = {}
        complete = serial[: serial.rfind("\n") + 1]
        # Each boot reports its own missing modules; check every boot's log separately.
        boot_logs = [b for b in re.split(r"(?m)^(?=rst:0x)", complete) if b.strip()]
        for index, boot_log in enumerate(boot_logs, 1):
            boot_policy = policy
            if args.case == "control" and index == 3 and default_eight:
                # This boot is intentionally reset during the initial moving
                # module scan. Require a valid prefix, not eight diagnostics for
                # operations the interrupted firmware never reached.
                pattern = r"(?m)^Error writing data to module (\d+), error code: 2\r?$"
                missing = [int(addr) for addr in re.findall(pattern, boot_log)]
                if missing != list(range(32, 32 + len(missing))) or len(missing) > 8:
                    raise RuntimeError("invalid partial absent-module scan before reset")
                result["interrupted_boot_missing_scan"] = missing
                boot_log = re.sub(pattern, "", boot_log)
                boot_policy = {**policy, "default_missing_bus1": False}
            serial_errors(boot_log, boot_policy, KnownSerial(), observed)
        result["tolerated_serial"] = [{"regex": k, **v} for k, v in observed.items()]
        result.update(passed=True, returncode=returncode)
    except Exception as error:
        result.update(passed=False, error=f"{type(error).__name__}: {error}")
        if run.process.poll() is None:
            run.stop()
    result["wall_seconds"] = round(time.monotonic() - started, 3)
    (run.out / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in {"build", "control"}}, indent=2))
    return 0 if result["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--build", type=Path, required=True, help="native/build.py output directory"
    )
    parser.add_argument("--hardware", default="esp32-8-single")
    parser.add_argument("--case", choices=CASES, default="boot")
    parser.add_argument("--speed", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=120.0, help="wall-clock seconds")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    return verify(args)


if __name__ == "__main__":
    raise SystemExit(main())
