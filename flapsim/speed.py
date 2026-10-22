"""Audited native clock-rate commands, serialized with model/lifecycle controls."""

import json
import math

from flapsim.control import Unavailable


def valid_speed(value):
    return type(value) in {int, float} and 0.25 <= value <= 50 and math.isfinite(value)


class SpeedController:
    def __init__(self, client, initial):
        self.client = client
        self.current = initial
        self.receipt = None

    def _read(self, command=None):
        packet = b"FLAPSIM_SPEED/1"
        if command is not None:
            packet += b" " + json.dumps(command, separators=(",", ":")).encode()
        reply = self.client._exchange(packet)
        if not isinstance(reply, dict) or set(reply) != {"speed", "state"}:
            raise ValueError("invalid native speed reply")
        if not valid_speed(reply["speed"]) or (command and reply["speed"] != command["speed"]):
            raise ValueError("native rate does not match requested speed")
        self.client._validate_snapshot(reply["state"])
        self.current = reply["speed"]
        return reply

    def metadata(self):
        return {"current": self.current, "min": 0.25, "max": 50, "runtime": True}

    def apply(self, command, journal):
        client = self.client
        if not client.lock.acquire(blocking=False):
            return {"error": "backend_busy"}
        try:
            if command and self.receipt and command["id"] <= self.receipt["command"]["id"]:
                if command == self.receipt["command"]:
                    return self.receipt
                return {"error": "stale_or_reused_id"}
            if client.closed:
                return {"error": "backend_unavailable"}
            if client.power and client.power.state != "on":
                if command:
                    return {"error": "controller_off"}
                return self.metadata()
            stage = "intent_audit"
            if command:
                self.receipt = {"command": command, "status": "unknown"}
            try:
                if command:
                    journal({"phase": "intent", "kind": "speed", "command": command})
                stage = "speed_exchange"
                reply = self._read(command)
                if command is None:
                    return self.metadata()
                state = reply["state"]
                receipt = {
                    "command": command,
                    "status": "applied",
                    "speed": reply["speed"],
                    "guest_ns": state["guest_ns"],
                    "sequence": state["sequence"],
                    "boot_generation": state["guest_resets"],
                    "fidelity": "native-virtual-clock",
                }
                stage = "result_audit"
                journal({"phase": "result", "kind": "speed", "command": command, "result": receipt})
                self.receipt = receipt
                return receipt
            except Exception as error:
                client.closed = True
                client.connection.close()
                if command:
                    self.current = None
                    try:
                        journal(
                            {
                                "phase": "unknown",
                                "kind": "speed",
                                "command": command,
                                "stage": stage,
                                "error": type(error).__name__,
                                "channel": "closed",
                            }
                        )
                    except Exception:
                        pass
                raise Unavailable("speed result unavailable") from error
        finally:
            client.lock.release()
