"""Controller power approximation, independent of external module power."""

import math
import socket
import struct
import threading
import time

from flapsim.control import Unavailable
from flapsim.lifecycle import qmp_commands, qmp_reset
from flapsim.process import stop_group


def observed_boot(client, before):
    deadline = time.monotonic() + 3
    while True:
        after = client._validate_snapshot(client._exchange(b"FLAPSIM_STATE/1"))
        if after["guest_resets"] != before["guest_resets"]:
            if after["guest_resets"] != before["guest_resets"] + 1:
                raise ValueError("unexpected power-on generation")
            return after
        if time.monotonic() >= deadline:
            raise TimeoutError("power-on not observed")
        time.sleep(0.01)


class QemuPower:
    fidelity = "qmp-stop-reset-cont"

    def __init__(self, path):
        self.path = path

    def off(self, client):
        status = qmp_commands(self.path, ["stop", "query-status"])[-1]
        if status.get("running") is not False:
            raise ValueError("QEMU did not stop")
        frozen = client._validate_snapshot(client._exchange(b"FLAPSIM_STATE/1"))
        time.sleep(0.05)
        if client._validate_snapshot(client._exchange(b"FLAPSIM_STATE/1")) != frozen:
            raise ValueError("QEMU state advanced while off")
        return frozen

    def on(self, client, frozen):
        qmp_reset(self.path)
        after = observed_boot(client, frozen)
        if after["modules"] != frozen["modules"] or after["counters"] != frozen["counters"]:
            raise ValueError("QEMU power-on changed external state before execution")
        status = qmp_commands(self.path, ["cont", "query-status"])[-1]
        if status.get("running") is not True:
            raise ValueError("QEMU did not resume")
        return after


class NativePower:
    """The runner's process handle, including intentionally reaped off periods.

    Only power operations and teardown take owner_lock; poll remains nonblocking
    so scenario host deadlines and cancellation remain effective during off/on.
    The caller holds the model client's single-flight lock during transitions.
    """

    fidelity = "native-checkpoint-restart"

    def __init__(self, process, launch):
        self.current = process
        self.launch = launch
        self.owner_lock = threading.Lock()
        self.mode = "on"
        self.closing = False
        self.resume = None
        self.reaped = False
        self.history = [{"pid": process.pid}]

    @property
    def pid(self):
        return self.current.pid

    @property
    def returncode(self):
        return self.current.returncode

    def poll(self):
        if not self.closing and self.mode in {"off", "transition"}:
            return None
        return self.current.poll()

    def off(self, client):
        with self.owner_lock:
            if self.closing:
                raise ValueError("native runner is shutting down")
            self.mode = "transition"
            try:
                reply = client._exchange(b"FLAPSIM_POWER_OFF/1")
                if not isinstance(reply, dict) or set(reply) != {"state", "resume"}:
                    raise ValueError("invalid native checkpoint")
                frozen = client._validate_snapshot(reply["state"])
                if not isinstance(reply["resume"], str):
                    raise ValueError("invalid native resume identity")
                parts = reply["resume"].split(":")
                if (
                    len(parts) not in {4, 5}
                    or not all(p.isascii() and p.isdecimal() for p in parts)
                    or not 2 <= int(parts[0]) <= 2**32 - 1
                    or parts[1] != "1"
                    or int(parts[2]) != frozen["guest_ns"]
                    or parts[3] not in {"0", "1"}
                ):
                    raise ValueError("invalid native resume identity")
                if len(parts) == 5:
                    bits = int(parts[4])
                    if bits > 2**64 - 1:
                        raise ValueError("invalid native resume rate")
                    speed = struct.unpack("d", struct.pack("Q", bits))[0]
                    if not math.isfinite(speed) or not 0.25 <= speed <= 50:
                        raise ValueError("invalid native resume rate")
                if self.current.wait(timeout=1) != 0:
                    raise ValueError("native checkpoint process failed")
                stop_group(self.current)
                self.reaped = True
                self.history[-1]["returncode"] = self.current.returncode
                self.resume = reply["resume"]
                self.mode = "off"
                return frozen
            except BaseException:
                self.mode = "unknown"
                raise

    def on(self, client, frozen):
        with self.owner_lock:
            if self.closing or self.resume is None:
                raise ValueError("native runner cannot resume")
            self.mode = "transition"
            parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            try:
                self.current = self.launch(child, self.resume)
                self.reaped = False
                self.history.append({"pid": self.current.pid})
                child.close()
                client.connection.close()
                client.connection = parent
                parent.settimeout(0.5)
                after = observed_boot(client, frozen)
                self.mode = "on"
                return after
            except BaseException:
                parent.close()
                self.mode = "unknown"
                raise
            finally:
                child.close()

    def stop(self):
        with self.owner_lock:
            self.closing = True
            if self.reaped:
                return self.current.returncode
            result = stop_group(self.current)
            self.reaped = True
            self.history[-1]["returncode"] = result
            return result


class PowerController:
    """Audited, latest-only power receipts in a separate monotonic ID namespace."""

    def __init__(self, client, backend):
        self.client, self.backend = client, backend
        self.state, self.frozen, self.receipt = "on", None, None
        client.power = self

    def metadata(self):
        result = {"state": self.state, "fidelity": self.backend.fidelity}
        if self.state == "off":
            result["snapshot"] = self.frozen
        return result

    def apply(self, command, journal):
        client = self.client
        if not client.lock.acquire(blocking=False):
            return {"error": "backend_busy"}
        try:
            if self.receipt and command["id"] <= self.receipt["command"]["id"]:
                if command == self.receipt["command"]:
                    return self.receipt
                return {"error": "stale_or_reused_id"}
            if client.closed or self.state == "unknown":
                return {"error": "backend_unavailable"}
            self.receipt = {"command": command, "status": "unknown"}
            stage = "intent_audit"
            try:
                journal({"phase": "intent", "kind": "power", "command": command})
                stage = "power_transition"
                target = "on" if command["on"] else "off"
                if target == self.state:
                    snapshot = (
                        self.frozen
                        if target == "off"
                        else client._validate_snapshot(client._exchange(b"FLAPSIM_STATE/1"))
                    )
                elif target == "off":
                    snapshot = self.backend.off(client)
                    self.frozen = snapshot
                else:
                    snapshot = self.backend.on(client, self.frozen)
                result = {
                    "command": command,
                    "status": "applied",
                    "power_state": target,
                    "boot_generation": snapshot["guest_resets"],
                    "guest_ns": snapshot["guest_ns"],
                    "sequence": snapshot["sequence"],
                    "fidelity": self.backend.fidelity,
                }
                stage = "result_audit"
                journal({"phase": "result", "kind": "power", "command": command, "result": result})
                self.state, self.receipt = target, result
                return result
            except Exception as error:
                # Any failure after admission can follow a real power transition.
                # Poison even unexpected adapter errors, never attempt another launch.
                self.state = "unknown"
                client.closed = True
                client.connection.close()
                try:
                    journal(
                        {
                            "phase": "unknown",
                            "kind": "power",
                            "command": command,
                            "stage": stage,
                            "error": type(error).__name__,
                            "channel": "closed",
                        }
                    )
                except Exception:
                    pass
                raise Unavailable("power result unknown") from error
        finally:
            client.lock.release()
