#!/usr/bin/env python3
"""Controlled native-mode child for runner tests; never used as firmware verification."""

import json
import os
import socket
import sys
import time
from pathlib import Path

config = json.loads(Path(sys.argv[sys.argv.index("--config") + 1]).read_text())
model = json.loads(Path(config["model_config"]).read_text())
out = Path(config["serial_log"]).parent
mode = os.environ.get("FLAPSIM_FAKE_MODE", "success")
boot = int(os.environ.get("FLAPSIM_FAKE_BOOT", "1"))
(out / f"boot-{boot}.json").write_text(json.dumps({"pid": os.getpid(), "config": config}))
if mode == "exit":
    sys.exit(17)
if mode == "hang":
    while True:
        time.sleep(1)
if mode == "tamper":
    Path(os.environ["FLAPSIM_FAKE_SOURCE"]).write_bytes(b"mutated executable")
if mode == "reexec" and boot == 1:
    # An emulated reset: the same process image is replaced and must keep the listener.
    os.execve(sys.executable, [sys.executable, *sys.argv], {**os.environ, "FLAPSIM_FAKE_BOOT": "2"})

counters = dict.fromkeys(["forward", "backward", "stalls", "writes", "reads", "anomalies"], 0)
state = {
    "schema_version": 1,
    "guest_ns": 1,
    "sequence": boot,
    "movement_id": 1,
    "settlement_id": 1,
    "settled": True,
    "event": "move_finished",
    "counters": counters,
    "text": "X" * len(model["modules"]),
    "rows": ["", ""],
    "modules": [],
}
for module in model["modules"]:
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
outputs = model["outputs"]
with open(config["serial_log"], "a") as serial:
    serial.write(f"flapsim native boot {boot}\r\n=== Split Flap Display ===\r\n")
Path(outputs["timeline"]).write_text(json.dumps(state) + "\n")
Path(outputs["state"]).write_text(json.dumps(state))
Path(outputs["activity"]).write_text(json.dumps(state))

server = socket.socket(fileno=config["http_fd"])
while True:
    connection, _ = server.accept()
    with connection:
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = connection.recv(4096)
            if not chunk:
                break
            request += chunk
        body = json.dumps({"version": "v9.9.9", "boot": boot}).encode()
        connection.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
