#!/usr/bin/env python3
"""Controlled OS child for lifecycle tests; never used as firmware verification."""

import json
import os
import re
import signal
import socket
import sys
import threading
import time
from pathlib import Path

config = json.loads(Path(os.environ["FLAPSIM_CONFIG"]).read_text())
out = Path(config["outputs"]["state"]).parent
mode = os.environ.get("FLAPSIM_FAKE_MODE", "success")
if mode in {"tree-hang", "leader-exit"}:
    child = os.fork()
    if child == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(1)
    (out / "descendant.pid").write_text(str(child))
    if mode == "leader-exit":
        sys.exit(17)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode == "exit":
    sys.exit(17)
if mode == "hang":
    while True:
        time.sleep(1)

nic = sys.argv[sys.argv.index("-nic") + 1]
port = int(re.search(r"127\.0\.0\.1:(\d+)-", nic)[1])
http = socket.socket()
try:
    http.bind(("127.0.0.1", port))
    http.listen()
except OSError:
    print("Could not set up host forwarding rule", flush=True)
    sys.exit(1)

control = sys.argv[sys.argv.index("-qmp") + 1].split(",")[0].removeprefix("unix:")
qmp = socket.socket(socket.AF_UNIX)
qmp.bind(control)
qmp.listen()


def control_server():
    while True:
        connection, _ = qmp.accept()
        try:
            with connection, connection.makefile("rwb") as stream:
                stream.write(b'{"QMP":{"version":{}}}\n')
                stream.flush()
                for line in stream:
                    request = json.loads(line)
                    if mode == "slow-qmp":
                        time.sleep(0.25)
                    reply = (
                        {} if request["execute"] == "qmp_capabilities" else {"status": "running"}
                    )
                    stream.write(
                        json.dumps({"return": reply, "id": request["id"]}).encode() + b"\n"
                    )
                    stream.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


threading.Thread(target=control_server, daemon=True).start()
flash_arg = sys.argv[sys.argv.index("-drive") + 1]
flash = Path(flash_arg.removeprefix("file=").split(",if=")[0].replace(",,", ","))
flash_bytes = flash.read_bytes()
(out / "flash-observed.json").write_text(
    json.dumps({"size": len(flash_bytes), "last": flash_bytes[-1], "first": flash_bytes[0]})
)
with flash.open("r+b") as working:
    working.seek(-1, 2)
    working.write(b"R")
if mode == "tamper":
    Path(os.environ["FLAPSIM_FAKE_SOURCE"]).write_bytes(b"mutated source")
if mode != "no-output":
    counters = dict.fromkeys(["forward", "backward", "stalls", "writes", "reads", "anomalies"], 0)
    state = {
        "schema_version": 1,
        "guest_ns": 1,
        "sequence": 1,
        "movement_id": 1,
        "settlement_id": 1,
        "settled": True,
        "event": "move_finished",
        "counters": counters,
        "text": "X" * len(config["modules"]),
        "rows": ["", ""],
        "modules": [],
    }
    for module in config["modules"]:
        state["rows"][module["bus"]] += "X"
        state["modules"].append(
            {
                "bus": module["bus"],
                "addr": module["addr"],
                "angle": 1328,
                "latch": 65505,
                "phase": -1,
                "hall": False,
                "energized": False,
                "char": "X",
                "counters": counters,
            }
        )
    if mode in {"fault", "anomaly"}:
        counter = "stalls" if mode == "fault" else "anomalies"
        state["modules"][0]["counters"] = {**counters, counter: 1}
        state["counters"] = {**counters, counter: 1}
    (out / "serial.log").write_text("=== Split Flap Display ===\n")
    (out / "timeline.jsonl").write_text(json.dumps(state) + "\n")
    activity = {**state, "settled": mode != "stale"}
    # Match the real model's publication contract: readers must never observe
    # a newly created but empty snapshot. Keep intentional malformed output.
    for name, payload in [
        ("state.json", json.dumps(state) if mode != "partial" else "{"),
        ("activity.json", json.dumps(activity)),
    ]:
        temporary = out / (name + ".tmp")
        temporary.write_text(payload)
        temporary.replace(out / name)
    if mode == "bad-timeline":
        (out / "timeline.jsonl").write_text("{")
while True:
    time.sleep(1)
