"""Real firmware: inject an evidenced fault, check unaffected modules, then recover."""

import argparse
import json
import re
import signal
import urllib.error
import urllib.request
from pathlib import Path

from flapsim.baseline import load_known_issues
from flapsim.executor import excused
from flapsim.faults import DEFAULT_FAULTS
from flapsim.runner import run
from flapsim.scenario import serial_errors
from flapsim.state import display_aligned
from tools.verify_native import KnownSerial

CASES = {
    "hall-active": {"sensor": "stuck_active"},
    "hall-inactive": {"sensor": "stuck_inactive"},
    "motor-dead": {"motor": "dead"},
    "absent": {"i2c": "missing"},
}


def interrupted_reset_lines(text, observed_resets):
    """Keep reset-interrupted UART fragments as evidence, not complete diagnostics.

    Only a literal prefix of the known Preferences begin diagnostic can be
    classified this way, and never more of them than the controller resets the
    probe observed. The tear can land anywhere after the "[E][" tag. The
    fragment either shares its line with the pinned ESP32 ROM banner or ends
    its line with the banner on the next non-empty line (or the capture ending
    first); what follows is kept in the evidence. Complete errors and other
    prefixes remain subject to the ordinary serial checks.
    """
    canonical = "Preferences.cpp:50] begin(): nvs_open failed: NOT_FOUND"
    banner = "ets Jul 29 2019 12:21:46"
    fragments, lines = [], []
    source = text.splitlines()

    def interrupted(prefix):
        match = re.fullmatch(r"\[\s*\d+\]\[E\]\[(.*)", prefix)
        return bool(match) and len(match[1]) < len(canonical) and canonical.startswith(match[1])

    def record(fragment, following=None):
        entry = {"kind": "reset_interrupted_diagnostic", "fragment": fragment}
        if following is not None:
            entry["followed_by"] = following[:80]
        fragments.append(entry)
        if len(fragments) > observed_resets:
            raise RuntimeError("more interrupted diagnostics than observed controller resets")

    for index, line in enumerate(source):
        before, separator, after = line.partition(banner)
        if separator and interrupted(before):
            record(before)
            line = banner + after
        elif not separator and interrupted(line):
            following = next((x for x in source[index + 1 :] if x.strip()), None)
            if following is None or following.startswith(banner):
                record(line, following or "")
                continue
        lines.append(line)
    return "\n".join(lines), fragments


def unexcused_faults(counters, known, serial):
    """Anomalies are never excused; stalls only as far as the known-issues baseline explains."""
    faulty = counters["anomalies"] or counters["stalls"]
    return bool(faulty) and excused(counters, known, serial) is None


class FaultProbe:
    timeout = 600
    document = {"version": 1, "mode": "fault-probe", "timeout": 600}

    def __init__(self, case, mode, hardware, known=None):
        self.case = case
        self.known = known
        self.evidence = {"case": case, "complete": False}
        if mode == "native":
            # Match the real configured topology, so every additional NACK is attributable.
            config = {"ssid": "flapsim-faults", "password": "probe-only"}
            if "8-" in hardware:
                config.update(wire1Addresses="", wire1Offsets="")
            self.native_setup = {"services": [], "clock": None, "nvs": {"config": config}}

    def execute(self, context):
        manifest = json.loads((context.out / "run.json").read_text())
        endpoint = manifest["control"]["endpoint"]
        token = (context.out / "control-token").read_text().strip()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.out = context.out
        native = manifest["mode"] == "native"
        self.native = native

        def unexcused(counters):
            # Known emulator scheduling artifacts (#14) can lose coil patterns and stall drums.
            path = context.out / "serial.log"
            serial = path.read_text(errors="replace") if path.exists() else ""
            return unexcused_faults(counters, self.known, serial)

        def record():
            (context.out / "fault-verification.json").write_text(
                json.dumps(self.evidence, indent=2) + "\n"
            )

        def api(path="/state", body=None, expected=200):
            context.checkpoint()
            request = urllib.request.Request(
                endpoint + "/api/emu" + path,
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                data=None if body is None else json.dumps(body).encode(),
            )
            try:
                response = opener.open(request, timeout=12)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                result = json.load(response)
                if response.status != expected:
                    raise RuntimeError(f"{path}: HTTP {response.status}: {result}")
                return result

        def patch(faults, identifier, revision):
            command = {"id": identifier, "expected_revision": revision, "patch": {"faults": faults}}
            while True:
                # Busy is an unadmitted transaction-boundary rejection, safe to retry.
                context.checkpoint()
                request = urllib.request.Request(
                    endpoint + "/api/emu/modules/0/32/mech",
                    headers={
                        "Authorization": "Bearer " + token,
                        "Content-Type": "application/json",
                    },
                    data=json.dumps(command).encode(),
                )
                try:
                    response = opener.open(request, timeout=12)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    result = json.load(response)
                    if response.status == 202:
                        return result
                    if response.status != 409 or result.get("error") != "model_busy":
                        raise RuntimeError(f"fault patch failed: {result}")
                context.pause()

        initial = api()
        if not initial["capabilities"]["set_fault"]:
            raise RuntimeError("fault capability missing")
        changed = patch(CASES[self.case], 1, 0)
        # A retained operation is queryable and a same-ID retry never reapplies.
        if api("/operations/1")["operation"] != changed["operation"]:
            raise RuntimeError("fault receipt unavailable")
        self.evidence["applied"] = changed
        frozen_angle = api()["state"]["modules"][0]["angle"]
        frozen_latch = api()["state"]["modules"][0]["latch"]
        while True:
            state = api()["state"]
            target = state["modules"][0]
            siblings = state["modules"][1:]
            healthy = all(
                not m["energized"]
                and m["char"] == (" " if native else "X")
                and abs(m["flap_error"]) <= 0.25
                and m["counters"]["reads"] > 0
                for m in siblings
            )
            effects = target["fault_counters"]
            if self.case == "motor-dead":
                observed = effects["blocked_steps"] > 0 and target["angle"] == frozen_angle
            elif self.case == "absent":
                observed = effects["nacks"] > 0 and target["latch"] == frozen_latch
            else:
                # The snapshot is physical truth; events count a changed firmware read.
                observed = effects["events"] > 0 and target["counters"]["reads"] > 0
            if healthy and observed:
                if unexcused(state["counters"]):
                    raise RuntimeError("unexpected model faults alongside injection")
                break
            context.pause()
        self.evidence["observed"] = state
        record()
        reset = api("/reset", {"id": 1}, 202)
        if api()["state"]["modules"][0]["faults"] != target["faults"]:
            raise RuntimeError("reset cleared fault overlay")
        off = api("/power", {"id": 1, "on": False}, 202)
        on = api("/power", {"id": 2, "on": True}, 202)
        resumed = api()["state"]
        if (
            on["boot_generation"] != off["boot_generation"] + 1
            or resumed["modules"][0]["faults"] != target["faults"]
        ):
            raise RuntimeError("power-on lost fault overlay")
        if api("/operations/1")["operation"] != changed["operation"]:
            raise RuntimeError("lifecycle transition lost fault receipt")
        self.evidence["lifecycle"] = {"reset": reset, "off": off, "on": on, "resumed": resumed}
        self.evidence["cleared"] = patch(DEFAULT_FAULTS, 2, 1)
        recovery = api("/reset", {"id": 2}, 202)
        self.evidence["recovery_reset"] = recovery
        text = (" " if native else "X") * len(context.hardware["modules"])
        while True:
            state = api()["state"]
            homed = True
            if native:
                records = [
                    json.loads(line)
                    for line in (context.out / "timeline.jsonl")
                    .read_text()
                    .splitlines(keepends=True)
                    if line.endswith("\n")
                ]
                homed = any(
                    r["event"] == "move_finished"
                    and r["text"].strip() == "OK"
                    and recovery["sequence"] < r["sequence"] < state["sequence"]
                    for r in records
                )
            if (
                homed
                and state["settled"]
                and state["text"] == text
                and state["sequence"] > recovery["sequence"]
                and display_aligned(state, text)
            ):
                if unexcused(state["counters"]):
                    raise RuntimeError("unexpected model faults during recovery")
                break
            context.pause()
        self.evidence["final"] = state
        self.evidence["complete"] = True
        record()
        return {"state": state}

    def verify_final(self, out, hardware):
        text = (out / "serial.log").read_text(errors="replace")
        if not self.native:
            lifecycle = self.evidence["lifecycle"]
            resets = {
                r["boot_generation"]
                for r in [lifecycle["reset"], lifecycle["on"], self.evidence["recovery_reset"]]
            }
            text, fragments = interrupted_reset_lines(text, len(resets))
            self.evidence["reset_interrupted_diagnostics"] = fragments
            (out / "fault-verification.json").write_text(json.dumps(self.evidence, indent=2) + "\n")
        expected = "Error writing data to module 32, error code: 2"
        count = text.splitlines().count(expected)
        if self.case == "absent":
            if (
                not 1 <= count <= 3
                or not self.evidence["observed"]["modules"][0]["fault_counters"]["nacks"]
            ):
                raise RuntimeError("missing/excess firmware NACK evidence")
            text = "\n".join(line for line in text.splitlines() if line != expected)
        serial_errors(text, {"fresh_nvs": True}, self.known or KnownSerial())
        if not self.evidence["complete"]:
            raise RuntimeError("incomplete fault proof")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["native", "qemu"], required=True)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--native-build", type=Path)
    parser.add_argument("--firmware", type=Path)
    parser.add_argument("--qemu-prefix", type=Path, default=Path("build/qemu/install"))
    parser.add_argument("--hardware", default="esp32-16-dual")
    parser.add_argument(
        "--known-issues",
        type=Path,
        help="baseline whose known firmware serial diagnostics are tolerated (QEMU golden runs)",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.firmware_src = None
    args.speed, args.scenario, args.seed = 10, "boot", 7
    args.timeout, args.trace, args.control_port, args.timing = 600, False, 0, "ci"
    received = []

    def interrupted(signum, frame):
        received.append(signum)

    previous = {s: signal.signal(s, interrupted) for s in [signal.SIGINT, signal.SIGTERM]}
    try:
        code = run(
            args,
            executor=FaultProbe(
                args.case,
                args.mode,
                args.hardware,
                load_known_issues(args.known_issues) if args.known_issues else None,
            ),
            interrupt=lambda: received[0] if received else None,
        )
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    print((args.out / "run.json").read_text())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
