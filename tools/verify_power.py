"""Real-firmware controller power probe through the public runner-owned API."""

import argparse
import json
import signal
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

from flapsim.process import survives
from flapsim.runner import run
from flapsim.state import display_aligned
from tools.verify_native import records as timeline_records


class PowerProbe:
    timeout = 300
    document = {"version": 1, "mode": "power-probe", "timeout": 300}

    def __init__(self, end_off=False, mode="qemu", off_exit="success"):
        self.end_off = end_off
        self.off_exit = off_exit
        if mode == "native":
            self.native_setup = {
                "services": [],
                "clock": None,
                "nvs": {"config": {"ssid": "flapsim-power", "password": "probe-only"}},
            }

    def execute(self, context):
        manifest = json.loads((context.out / "run.json").read_text())
        endpoint = manifest["control"]["endpoint"]
        token = (context.out / "control-token").read_text().strip()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        evidence = {"complete": False, "cycles": []}
        is_native = manifest["mode"] == "native"

        def record():
            (context.out / "power-verification.json").write_text(
                json.dumps(evidence, indent=2) + "\n"
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

        def records():
            return timeline_records(context.out)

        def settled_after(sequence):
            while True:
                value = api()
                state = value["state"]
                if is_native:
                    homed = [
                        r
                        for r in records()
                        if r["sequence"] > sequence
                        and r["event"] == "move_finished"
                        and r["text"].strip() == "OK"
                    ]
                    done = (
                        homed
                        and state["sequence"] > homed[0]["sequence"]
                        and not state["text"].strip()
                    )
                else:
                    text = "X" * len(context.hardware["modules"])
                    done = (
                        state["sequence"] > sequence
                        and state["text"] == text
                        and display_aligned(state, text)
                    )
                if state["settled"] and done:
                    if state["counters"]["stalls"] or state["counters"]["anomalies"]:
                        raise RuntimeError("model faults after power-on")
                    return value
                context.pause()

        if not is_native:
            timing = api("/speed")
            if timing["runtime"] or timing["timing"] != manifest["timing"]:
                raise RuntimeError("QEMU did not report its fixed startup timing")
            rejected = api("/speed", {"id": 1, "speed": 2}, 409)
            if rejected["error"] != "unsupported_capability":
                raise RuntimeError("QEMU incorrectly accepted live speed")
            evidence["fixed_timing"] = timing
        while api()["state"]["settled"]:
            context.pause()
        before = api()
        if not before["capabilities"]["power"]:
            raise RuntimeError("power capability is unavailable")
        evidence["moving"] = before
        record()
        initial = settled_after(0)
        edit = api(
            "/modules/0/32/mech",
            {
                "id": 1,
                "expected_revision": 0,
                "patch": {
                    "sensor_window": initial["state"]["modules"][0]["mechanics"]["sensor_window"]
                    + 1
                },
            },
            202,
        )
        evidence["mechanics"] = edit
        if is_native:
            (context.out / "littlefs" / "power-probe.txt").write_text("retain this file")
        for cycle in range(2):
            if cycle:
                while api()["state"]["settled"]:
                    context.pause()
            off_command = {"id": 2 * cycle + 1, "on": False}
            off = api("/power", off_command, 202)
            frozen = api()
            if frozen["live"] or frozen["power_state"] != "off":
                raise RuntimeError("off-state snapshot claims live execution")
            if frozen["state"]["last_operation"] != edit["operation"]:
                raise RuntimeError("power transition lost the mechanics receipt")
            if is_native:
                nvs = (context.out / "nvs.json").read_bytes()
            for _ in range(3):
                time.sleep(0.1)
                context.checkpoint()
                if api() != frozen:
                    raise RuntimeError("controller/guest clock advanced while off")
                if is_native and (context.out / "nvs.json").read_bytes() != nvs:
                    raise RuntimeError("NVS changed while controller was off")
            if api("/power", off_command, 202) != off:
                raise RuntimeError("duplicate power-off changed the receipt")
            api("/reset", {"id": 100 + cycle}, 409)
            api(
                "/modules/0/32/mech",
                {"id": 100 + cycle, "expected_revision": 1, "patch": {"sensor_window": 42}},
                409,
            )
            if api("/power/operations/" + str(off_command["id"])) != off:
                raise RuntimeError("power receipt unavailable while off")
            entry = {"off": off, "frozen": frozen}
            evidence["cycles"].append(entry)
            record()
            if self.end_off and cycle == 1:
                evidence["complete"] = True
                record()
                if self.off_exit == "timeout":
                    context.deadline = min(context.deadline, time.monotonic() + 0.05)
                    while True:
                        context.pause()
                if self.off_exit == "interrupt":
                    context.interrupt = lambda: signal.SIGTERM
                    context.checkpoint()
                return {"power_state": "off"}
            on_command = {"id": 2 * cycle + 2, "on": True}
            on = api("/power", on_command, 202)
            if on["boot_generation"] != off["boot_generation"] + 1:
                raise RuntimeError("power-on did not record exactly one reset")
            if api("/power", on_command, 202) != on:
                raise RuntimeError("power-on retry applied twice")
            entry["on"] = on
            record()
        final = settled_after(on["sequence"])
        if api("/operations/1")["operation"] != edit["operation"]:
            raise RuntimeError("power-on lost mechanics receipt")
        if is_native:
            saved = json.loads((context.out / "nvs.json").read_text())["namespaces"]["config"]
            if (
                saved["ssid"]["value"] != "flapsim-power"
                or saved["password"]["value"] != "probe-only"
            ):
                raise RuntimeError("firmware credentials did not survive power cycles")
            if (context.out / "littlefs" / "power-probe.txt").read_text() != "retain this file":
                raise RuntimeError("LittleFS was recreated on power-on")
            if context.serial().count("Reset reason: power-on") != 3:
                raise RuntimeError("wrong native power-on reset reason")
            # Resume occurs before firmware setup: compare exact checkpoint and
            # next guest_reset records rather than an older moving sample.
            items = records()
            for index, item in enumerate(items[:-1]):
                if item["event"] == "controller_power_off":
                    successor = items[index + 1]
                    if (
                        successor["event"] != "guest_reset"
                        or successor["modules"] != item["modules"]
                    ):
                        raise RuntimeError("native power-on changed checkpointed external state")
        evidence["final"] = final
        evidence["complete"] = True
        record()
        return {"state": final["state"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["native", "qemu"], required=True)
    parser.add_argument("--native-build", type=Path)
    parser.add_argument("--firmware", type=Path)
    parser.add_argument("--qemu-prefix", type=Path, default=Path("build/qemu/install"))
    parser.add_argument("--hardware", default="esp32-8-single")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--end-off", action="store_true")
    parser.add_argument(
        "--off-exit", choices=["success", "timeout", "interrupt"], default="success"
    )
    args = parser.parse_args()
    args.firmware_src = None
    args.speed, args.scenario, args.seed = 5, "boot", 7
    args.timeout, args.trace, args.control_port, args.timing = 300, False, 0, "ci"
    code = run(
        args,
        executor=PowerProbe(args.end_off or args.off_exit != "success", args.mode, args.off_exit),
    )
    print((args.out / "run.json").read_text())
    expected = {"success": 0, "timeout": 4, "interrupt": 128 + signal.SIGTERM}[args.off_exit]
    if code != expected:
        return 1
    manifest = json.loads((args.out / "run.json").read_text())
    port = int(manifest["control"]["endpoint"].rsplit(":", 1)[1])
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError("power listener survived shutdown")
    attempts = manifest["attempts"]
    processes = [p for a in attempts for p in a.get("power_processes", [a])]
    for process in processes:
        if survives(process["pid"], process.get("start_time")):
            raise RuntimeError("supervised process survived shutdown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
