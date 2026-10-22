"""Real-firmware probe: live state, audited mechanics, pause and controller reset."""

import argparse
import hashlib
import json
import signal
import socket
import time
import urllib.request
from pathlib import Path

from flapsim.baseline import load_known_issues
from flapsim.executor import BootExecutor, excused
from flapsim.hardware import ROOT
from flapsim.runner import run
from flapsim.state import display_aligned


def qmp(path, command):
    """Separate bounded connection; never confuse asynchronous events with replies."""
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(2)
        connection.connect(str(path))
        stream = connection.makefile("rb")
        try:
            for identifier, name in [("cap", "qmp_capabilities"), ("probe", command)]:
                connection.sendall(json.dumps({"execute": name, "id": identifier}).encode() + b"\n")
                while True:
                    line = stream.readline(65537)
                    if not line or len(line) > 65536:
                        raise RuntimeError("incomplete QMP probe reply")
                    message = json.loads(line)
                    if message.get("id") == identifier:
                        if "error" in message:
                            raise RuntimeError(f"QMP command failed: {message['error']}")
                        break
            return message["return"]
        finally:
            stream.close()


class ControlProbe:
    timeout = 300
    document = {"version": 1, "mode": "boot", "timeout": 300}

    def __init__(self, known_issues=None):
        self.known_issues = known_issues

    def execute(self, context):
        manifest = json.loads((context.out / "run.json").read_text())
        endpoint = manifest["control"]["endpoint"]
        token = (context.out / "control-token").read_text().strip()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        command = manifest["command"]
        qmp_path = command[command.index("-qmp") + 1].removeprefix("unix:").split(",")[0]
        evidence = {"complete": False}

        def record(stage, value):
            evidence[stage] = value
            temporary = context.out / "control-verification.json.tmp"
            temporary.write_text(json.dumps(evidence, indent=2) + "\n")
            temporary.replace(context.out / "control-verification.json")

        def snapshot():
            context.checkpoint()
            request = urllib.request.Request(
                endpoint + "/api/emu/state", headers={"Authorization": "Bearer " + token}
            )
            with opener.open(request, timeout=2) as response:
                value = json.load(response)
            if not value["capabilities"]["reset"] or not value["live"]:
                raise RuntimeError("incorrect control capabilities/freshness")
            return value

        # Require a moving state, which cannot be obtained from state.json.
        while (moving := snapshot())["state"]["settled"]:
            context.pause()
        record("moving", moving)
        boot = BootExecutor(
            240, {"expect_text": "X" * len(context.hardware["modules"])}, self.known_issues
        )
        settled = boot.execute(context)
        record("first_settlement", settled["state"])
        if boot.tolerated_stalls:
            record("tolerated_stalls", boot.tolerated_stalls)

        # The public API only reads. Test servicing a paused guest after the
        # display settles; arbitrary mid-motion QMP pause fidelity is separate.
        qmp(qmp_path, "stop")
        while qmp(qmp_path, "query-status")["status"] != "paused":
            context.pause()
        paused = snapshot()
        record("paused", paused)
        for _ in range(3):
            context.pause()
            if snapshot() != paused:
                raise RuntimeError("snapshot reads changed a paused model")

        def change(identifier, revision, patch):
            request = urllib.request.Request(
                endpoint + "/api/emu/modules/0/32/mech",
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                data=json.dumps(
                    {"id": identifier, "expected_revision": revision, "patch": patch}
                ).encode(),
            )
            with opener.open(request, timeout=2) as response:
                if response.status != 202:
                    raise RuntimeError("mechanics edit was not acknowledged")
                return json.load(response)

        original = paused["state"]["modules"][0]
        polarity = original["mechanics"]["sensor_polarity"]
        edited = change(
            1, 0, {"sensor_polarity": "active-low" if polarity == "active-high" else "active-high"}
        )
        record("mechanics_operation", edited)
        changed = snapshot()
        record("mechanics_changed", changed)
        module = changed["state"]["modules"][0]
        if module["angle"] != original["angle"] or module["hall"] == original["hall"]:
            raise RuntimeError("mechanics edit moved the drum or did not change electrical hall")
        if changed["state"]["mechanics_revision"] != 1:
            raise RuntimeError("mechanics revision did not advance")
        # A replay has the same receipt and changes neither counters nor timeline sequences.
        if change(1, 0, edited["operation"]["command"]["patch"]) != edited or snapshot() != changed:
            raise RuntimeError("replayed command applied more than once")
        # Exercise the public reset dispatcher while QEMU remains paused.
        before_reset = snapshot()
        record("before_reset", before_reset)

        def reset(identifier):
            request = urllib.request.Request(
                endpoint + "/api/emu/reset",
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                data=json.dumps({"id": identifier}).encode(),
            )
            with opener.open(request, timeout=7) as response:
                if response.status != 202:
                    raise RuntimeError("reset was not acknowledged")
                return json.load(response)

        reset_receipt = reset(1)
        record("reset_receipt", reset_receipt)
        if qmp(qmp_path, "query-status")["status"] != "paused":
            raise RuntimeError("public reset changed QEMU's paused state")
        if reset(1) != reset_receipt:
            raise RuntimeError("reset retry did not return the original receipt")
        while (after_reset := snapshot())["boot_generation"] == before_reset["boot_generation"]:
            context.pause()
        record("after_reset", after_reset)
        if after_reset["boot_generation"] != before_reset["boot_generation"] + 1:
            raise RuntimeError("reset was recorded more than once")
        if after_reset["state"]["modules"] != before_reset["state"]["modules"]:
            raise RuntimeError("controller reset changed external physical state")
        if after_reset["state"]["last_operation"] != edited["operation"]:
            raise RuntimeError("reset lost the mechanics operation")
        second_reset = reset(2)
        if second_reset["boot_generation"] != after_reset["boot_generation"] + 1:
            raise RuntimeError("second reset was not observed exactly once")
        record("second_reset", second_reset)
        restored = change(2, 1, {"sensor_polarity": polarity})
        record("mechanics_restored", restored)
        restored_state = snapshot()
        if restored_state["state"]["modules"] != paused["state"]["modules"]:
            raise RuntimeError("restoring mechanics did not preserve the external state")
        qmp(qmp_path, "cont")
        while True:
            final = snapshot()
            state = final["state"]
            if state["counters"]["stalls"] or state["counters"]["anomalies"]:
                tolerated = excused(state["counters"], self.known_issues, context.serial())
                if tolerated is None:
                    raise RuntimeError("model faults after reset")
                record("tolerated_stalls", tolerated)
            if (
                state["settled"]
                and state["settlement_id"] > before_reset["state"]["settlement_id"]
                and state["text"] == "X" * len(context.hardware["modules"])
                and display_aligned(state, state["text"])
            ):
                break
            context.pause()
        evidence["complete"] = True
        record("final", final)
        return settled


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--hardware", default="esp32-8-single")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--qemu-prefix", type=Path, default=ROOT / "build/qemu/install")
    parser.add_argument(
        "--known-issues", type=Path, help="golden baseline whose #14 entries may excuse stalls"
    )
    args = parser.parse_args()
    known_issues = None
    if args.known_issues:
        known_issues = load_known_issues(args.known_issues)
        known_issues.check_firmware(hashlib.sha256(args.firmware.read_bytes()).hexdigest())
    args.scenario, args.timing, args.seed = "boot", "ci", 7
    args.timeout, args.trace, args.control_port = 300, False, 0
    started = time.monotonic()
    received = []

    def interrupted(signum, frame):
        if not received:
            received.append(signum)

    previous = {s: signal.signal(s, interrupted) for s in [signal.SIGINT, signal.SIGTERM]}
    try:
        code = run(
            args,
            executor=ControlProbe(known_issues),
            interrupt=lambda: received[0] if received else None,
        )
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    print(f"Control probe exit {code}; {time.monotonic() - started:.3f}s; evidence: {args.out}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
