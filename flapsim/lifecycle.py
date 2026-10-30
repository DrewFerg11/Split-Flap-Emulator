"""Bounded, runner-owned controller resets; external mechanics remain in the model."""

import json
import socket
import time

from flapsim.control_channel import Unavailable


def qmp_commands(path, commands):
    """One hard reset, no automatic retry. Ignore events, match replies by ID."""
    deadline = time.monotonic() + 2
    results = []
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(2)
        connection.connect(str(path))
        with connection.makefile("rb") as stream:
            total = 0
            for identifier, name in [
                ("cap", "qmp_capabilities"),
                *[(str(i), name) for i, name in enumerate(commands)],
            ]:
                connection.sendall(json.dumps({"execute": name, "id": identifier}).encode() + b"\n")
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("reset QMP deadline")
                    connection.settimeout(remaining)
                    line = stream.readline(65537)
                    total += len(line)
                    if not line or total > 65536:
                        raise ValueError("incomplete or oversized QMP reset response")
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise ValueError("invalid QMP reset response")
                    if message.get("id") == identifier:
                        if "error" in message or not isinstance(message.get("return"), dict):
                            raise ValueError("QMP reset rejected")
                        results.append(message["return"])
                        break
    return results[1:]


def qmp_reset(path):
    """One hard reset, with no automatic retry."""
    qmp_commands(path, ["system_reset"])


class ResetController:
    """Latest-only receipts and monotonic IDs in a separate reset command namespace.

    The model channel lock serializes resets with reads/edits. Any uncertain
    outcome consumes the ID and poisons this child channel, never repeats reset.
    Receipt lookup remains possible independently of that channel.
    """

    def __init__(self, client, reset=None):
        self.client = client
        self.trigger = reset
        self.receipt = None

    def apply(self, command, journal):
        client = self.client
        if not client.lock.acquire(blocking=False):
            return {"error": "backend_busy"}
        try:
            if self.receipt is not None and command["id"] <= self.receipt["command"]["id"]:
                if command == self.receipt["command"]:
                    return self.receipt
                return {"error": "stale_or_reused_id"}
            if client.closed:
                return {"error": "backend_unavailable"}
            if client.power is not None and client.power.state != "on":
                return {"error": "controller_off"}
            stage = "intent_audit"
            self.receipt = {"command": command, "status": "unknown"}
            try:
                journal({"phase": "intent", "kind": "reset", "command": command})
                stage = "before_snapshot"
                before = client._validate_snapshot(client._exchange(b"FLAPSIM_STATE/1"))
                stage = "reset_dispatch"
                if self.trigger is None:
                    if client._exchange(b"FLAPSIM_RESET/1") != {"status": "accepted"}:
                        raise ValueError("native reset not accepted")
                else:
                    self.trigger()
                stage = "reset_observation"
                deadline = time.monotonic() + 3
                while True:
                    after = client._validate_snapshot(client._exchange(b"FLAPSIM_STATE/1"))
                    generation = after["guest_resets"]
                    if generation != before["guest_resets"]:
                        if generation != before["guest_resets"] + 1:
                            raise ValueError("unexpected reset generation")
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("reset was not observed")
                    time.sleep(0.01)
                receipt = {
                    "command": command,
                    "status": "applied",
                    "before_generation": before["guest_resets"],
                    "boot_generation": generation,
                    "guest_ns": after["guest_ns"],
                    "sequence": after["sequence"],
                    "fidelity": "qmp-hard-reset" if self.trigger else "native-software-reexec",
                }
                stage = "result_audit"
                journal({"phase": "result", "kind": "reset", "command": command, "result": receipt})
                self.receipt = receipt
                return receipt
            except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
                client.closed = True
                client.connection.close()
                try:
                    journal(
                        {
                            "phase": "unknown",
                            "kind": "reset",
                            "command": command,
                            "stage": stage,
                            "error": type(error).__name__,
                            "channel": "closed",
                        }
                    )
                except (OSError, ValueError, TypeError, KeyError, RecursionError):
                    pass
                raise Unavailable("reset result unknown") from error
        finally:
            client.lock.release()
