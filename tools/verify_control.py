"""Real-firmware probe: live snapshots during motion, pause and controller reset."""

import argparse
import json
import signal
import socket
import time
import urllib.request
from pathlib import Path

from flapsim.executor import BootExecutor
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

    def execute(self, context):
        manifest = json.loads((context.out / "run.json").read_text())
        endpoint = manifest["control"]["endpoint"]
        token = (context.out / "control-token").read_text().strip()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        command = manifest["command"]
        qmp_path = command[command.index("-qmp") + 1].removeprefix("unix:").split(",")[0]

        def snapshot():
            context.checkpoint()
            request = urllib.request.Request(
                endpoint + "/api/emu/state", headers={"Authorization": "Bearer " + token}
            )
            with opener.open(request, timeout=2) as response:
                value = json.load(response)
            if value["capabilities"]["reset"] or not value["live"]:
                raise RuntimeError("incorrect control capabilities/freshness")
            return value

        # Require a moving state, which cannot be obtained from state.json.
        while (moving := snapshot())["state"]["settled"]:
            context.pause()
        qmp(qmp_path, "stop")
        while qmp(qmp_path, "query-status")["status"] != "paused":
            context.pause()
        paused = snapshot()
        for _ in range(3):
            context.pause()
            if snapshot() != paused:
                raise RuntimeError("snapshot reads changed a paused model")
        qmp(qmp_path, "cont")
        settled = BootExecutor(
            240, {"expect_text": "X" * len(context.hardware["modules"])}
        ).execute(context)

        # Verify the owner-context channel and reset counter across an actual QMP
        # reset, without making reset a public HTTP capability in this first slice.
        qmp(qmp_path, "stop")
        while qmp(qmp_path, "query-status")["status"] != "paused":
            context.pause()
        before_reset = snapshot()
        qmp(qmp_path, "system_reset")
        while (after_reset := snapshot())["boot_generation"] == before_reset["boot_generation"]:
            context.pause()
        if after_reset["boot_generation"] != before_reset["boot_generation"] + 1:
            raise RuntimeError("reset was recorded more than once")
        if after_reset["state"]["modules"] != before_reset["state"]["modules"]:
            raise RuntimeError("controller reset changed external physical state")
        qmp(qmp_path, "cont")
        while True:
            final = snapshot()
            state = final["state"]
            if state["counters"]["stalls"] or state["counters"]["anomalies"]:
                raise RuntimeError("model faults after reset")
            if (
                state["settled"]
                and state["settlement_id"] > before_reset["state"]["settlement_id"]
                and state["text"] == "X" * len(context.hardware["modules"])
                and display_aligned(state, state["text"])
            ):
                break
            context.pause()
        evidence = {
            "moving": moving,
            "paused": paused,
            "before_reset": before_reset,
            "after_reset": after_reset,
            "final": final,
        }
        (context.out / "control-verification.json").write_text(
            json.dumps(evidence, indent=2) + "\n"
        )
        return settled


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--hardware", default="esp32-8-single")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--qemu-prefix", type=Path, default=ROOT / "build/qemu/install")
    args = parser.parse_args()
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
            args, executor=ControlProbe(), interrupt=lambda: received[0] if received else None
        )
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    print(f"Control probe exit {code}; {time.monotonic() - started:.3f}s; evidence: {args.out}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
