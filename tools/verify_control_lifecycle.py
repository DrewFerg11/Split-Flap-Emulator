"""Exercise bounded shutdown with an authenticated, deliberately unread WebSocket."""

import argparse
import json
import os
import signal
import socket
import time
from pathlib import Path

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from flapsim.process import survives
from flapsim.runner import run
from tools.control_evidence import verify_control_artifacts


class Probe:
    timeout = 30
    document = {"version": 1, "mode": "control-lifecycle", "timeout": 30}

    def __init__(self, case):
        self.case = case
        self.ws = None
        self.triggered = False

    def execute(self, context):
        manifest = json.loads((context.out / "run.json").read_text())
        control = manifest["control"]
        self.ws = connect(
            control["endpoint"].replace("http:", "ws:") + "/api/emu/stream", proxy=None, max_queue=1
        )
        self.ws.send(json.dumps({"token": (context.out / "control-token").read_text().strip()}))
        page = json.loads(self.ws.recv(timeout=3))
        if page["run_id"] != control["run_id"] or page["error"]:
            raise RuntimeError("authenticated stream did not start")
        # Keep the connection open and unread through child/server cleanup.
        self.triggered = True
        if self.case == "timeout":
            context.deadline = min(context.deadline, time.monotonic() + 0.15)
        elif self.case in {"sigint", "sigterm"}:
            os.kill(os.getpid(), signal.SIGINT if self.case == "sigint" else signal.SIGTERM)
        else:
            os.killpg(context.process.pid, signal.SIGKILL)
        while True:
            context.pause()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["native", "qemu"], required=True)
    parser.add_argument("--native-build", type=Path)
    parser.add_argument("--firmware", type=Path)
    parser.add_argument("--qemu-prefix", type=Path, default=Path("build/qemu/install"))
    parser.add_argument("--case", choices=["timeout", "sigint", "sigterm", "crash"], required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.firmware_src = None
    args.speed, args.scenario, args.seed = 5, "boot", 7
    args.timeout, args.trace, args.control_port, args.timing = 30, False, 0, "ci"
    args.hardware, args.i2c_history = "esp32-8-single", True
    probe = Probe(args.case)
    expected = {"timeout": 4, "sigint": 130, "sigterm": 143, "crash": 3}[args.case]
    received = []
    previous_handlers = {
        s: signal.signal(s, lambda number, _frame: received.append(number))
        for s in [signal.SIGINT, signal.SIGTERM]
    }
    try:
        code = run(args, executor=probe, interrupt=lambda: received[0] if received else None)
        manifest = json.loads((args.out / "run.json").read_text())
        if code != expected or manifest.get("exit_code") != expected or not probe.triggered:
            raise RuntimeError(f"expected exit {expected}, got {code}: {manifest.get('error')}")
        if args.case == "crash" and not manifest.get("error", "").startswith(
            "emulator exited early:"
        ):
            raise RuntimeError("crash probe failed before observing the intentionally killed child")
        proof = verify_control_artifacts(args.out, manifest)
        if probe.ws is None:
            raise RuntimeError("shutdown happened before a WebSocket connected")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                probe.ws.recv(timeout=max(0.01, deadline - time.monotonic()))
            except ConnectionClosed as closed:
                if closed.rcvd is None or closed.rcvd.code not in {1000, 1001, 1012}:
                    raise RuntimeError("stream shutdown lacked a clean close frame") from closed
                break
        else:
            raise RuntimeError("WebSocket survived runner shutdown")
        port = int(manifest["control"]["endpoint"].rsplit(":", 1)[1])
        with socket.socket() as listener:
            if listener.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError("control listener survived shutdown")
        for attempt in manifest["attempts"]:
            for process in attempt.get("power_processes", [attempt]):
                if survives(process["pid"], process.get("start_time")):
                    raise RuntimeError("owned child survived shutdown")
        (args.out / "control-lifecycle-verification.json").write_text(
            json.dumps(
                {"complete": True, "case": args.case, "expected_exit": expected, "evidence": proof},
                indent=2,
            )
            + "\n"
        )
        print(f"PASS: {args.mode} {args.case}; evidence: {args.out}")
        return 0
    finally:
        for number, handler in previous_handlers.items():
            signal.signal(number, handler)
        if probe.ws is not None:
            probe.ws.close()


if __name__ == "__main__":
    raise SystemExit(main())
