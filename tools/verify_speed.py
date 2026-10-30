"""Exercise native runtime rates and persistence through the public control API."""

import argparse
import json
import time
from pathlib import Path

from flapsim.runner import run
from flapsim.state import display_aligned
from tools.control_client import ControlClient


class SpeedProbe:
    timeout = 300
    document = {"version": 1, "mode": "speed-probe", "timeout": 300}

    def __init__(self, slow_timeout=False):
        self.slow_timeout = slow_timeout

    def execute(self, context):
        client = ControlClient(context, timeout=6)
        api = client.api
        evidence = {"complete": False, "rates": []}

        def record():
            (context.out / "speed-verification.json").write_text(
                json.dumps(evidence, indent=2) + "\n"
            )

        def measure(rate):
            start = time.monotonic()
            before = api()["state"]["guest_ns"]
            time.sleep(0.12)
            after = api()["state"]["guest_ns"]
            elapsed = time.monotonic() - start
            observed = (after - before) / 1e9 / elapsed
            evidence["rates"].append({"requested": rate, "observed": observed})
            record()
            if not 0.5 * rate <= observed <= 1.5 * rate:
                raise RuntimeError(f"clock rate {observed} does not match {rate}")

        for identifier, rate in enumerate([0.25, 20, 1.25, 12.5], 1):
            receipt = api("/speed", {"id": identifier, "speed": rate}, 202)
            if api("/speed")["current"] != rate or not api()["capabilities"]["speed"]:
                raise RuntimeError("effective speed/capability missing")
            if api("/speed", {"id": identifier, "speed": rate}, 202) != receipt:
                raise RuntimeError("speed retry changed receipt")
            measure(rate)
        generation = api()["boot_generation"]
        reset = api("/reset", {"id": 1}, 202)
        if reset["boot_generation"] != generation + 1 or api("/speed")["current"] != 12.5:
            raise RuntimeError("reset lost runtime speed")
        if api("/speed/operations/4") != receipt:
            raise RuntimeError("reset lost speed receipt")
        off = api("/power", {"id": 1, "on": False}, 202)
        if api("/speed")["current"] != 12.5:
            raise RuntimeError("off-state lost effective rate")
        api("/speed", {"id": 5, "speed": 2}, 409)
        on = api("/power", {"id": 2, "on": True}, 202)
        if on["boot_generation"] != off["boot_generation"] + 1:
            raise RuntimeError("power-on did not advance generation")
        if api("/speed")["current"] != 12.5:
            raise RuntimeError("power-on lost runtime speed")
        measure(12.5)
        if self.slow_timeout:
            api("/speed", {"id": 5, "speed": 0.25}, 202)
            context.deadline = min(context.deadline, time.monotonic() + 0.1)
            while True:
                context.pause()
        while True:
            state = api()["state"]
            text = "X" * len(context.hardware["modules"])
            if (
                state["settled"]
                and state["text"] == text
                and state["sequence"] > on["sequence"]
                and display_aligned(state, text)
            ):
                if state["counters"]["stalls"] or state["counters"]["anomalies"]:
                    raise RuntimeError("model faults after rate/reset/power transitions")
                evidence["complete"] = True
                evidence["final"] = state
                record()
                return {"state": state}
            context.pause()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-build", type=Path, required=True)
    parser.add_argument("--hardware", default="esp32-8-single")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--slow-timeout", action="store_true")
    args = parser.parse_args()
    args.mode, args.firmware_src = "native", None
    args.speed, args.scenario, args.seed = 5, "boot", 7
    args.timeout, args.trace, args.control_port, args.timing = 300, False, 0, "ci"
    started = time.monotonic()
    code = run(args, executor=SpeedProbe(args.slow_timeout))
    print((args.out / "run.json").read_text())
    if code != (4 if args.slow_timeout else 0):
        return 1
    if args.slow_timeout and time.monotonic() - started > 30:
        raise RuntimeError("slow guest defeated host deadline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
