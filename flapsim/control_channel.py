"""The runner's private request channel to the model owner (QEMU adapter or native child)."""

import copy
import json
import socket
import threading
import time

from flapsim.state import validate_state

MAX_SNAPSHOT = 64 * 1024


class Unavailable(RuntimeError):
    pass


class SnapshotClient:
    """One request in flight. A timeout poisons the channel so replies cannot be misattributed."""

    def __init__(self, connection, hardware):
        self.connection = connection
        self.hardware = hardware
        self.lock = threading.Lock()
        self.closed = False
        self.failure = None
        self.power = None
        # How long the model took to answer (#104): every valid exchange, and the last attempt.
        self.exchanges = 0
        self.slowest = 0.0
        self.elapsed = None
        connection.settimeout(0.5)

    def close(self):
        # Close admission immediately, including handlers that already captured
        # this client but are still uploading a body. An admitted request may
        # finish under the lock before its transport is closed.
        self.closed = True
        with self.lock:
            self.connection.close()

    def _validate_snapshot(self, state):
        validate_state(state, self.hardware, require_settled=False)
        if type(state.get("guest_resets")) is not int or state["guest_resets"] < 0:
            raise ValueError("invalid model reset count")
        for actual in state["modules"]:
            if (
                type(actual.get("coils")) is not int
                or actual["coils"] != (actual["latch"] >> 1) & 15
                or actual["energized"] != bool(actual["coils"])
            ):
                raise ValueError("invalid model coils")
        return state

    def latency(self):
        """Exchange timing for the run's evidence, or None before the first valid reply."""
        if not self.exchanges:
            return None
        return {"exchanges": self.exchanges, "slowest_ms": round(self.slowest * 1000, 1)}

    def _exchange(self, request):
        started = time.monotonic()
        try:
            self.connection.sendall(request)
            data, _, flags, _ = self.connection.recvmsg(MAX_SNAPSHOT)
        finally:
            self.elapsed = time.monotonic() - started
        if not data or flags & socket.MSG_TRUNC:
            raise ValueError("incomplete or oversized model reply")
        reply = json.loads(data)
        self.exchanges += 1
        self.slowest = max(self.slowest, self.elapsed)
        return reply

    def snapshot(self):
        return self.dispatch()

    def view(self):
        return self.dispatch(with_power=True)

    def dispatch(self, command=None, journal=None, *, with_power=False):
        if not self.lock.acquire(blocking=False):
            raise Unavailable("model request already in progress")
        try:
            if self.closed:
                raise Unavailable("model channel is unavailable")
            if self.power is not None and self.power.state == "off":
                if command is not None:
                    return {"error": "controller_off"}
                frozen = copy.deepcopy(self.power.frozen)
                return (frozen, "off") if with_power else frozen
            stage = "intent_audit"
            # The timing of this attempt only: a failure before the exchange has none.
            self.elapsed = None
            try:
                if command is not None:
                    journal({"phase": "intent", "command": command})
                request = (
                    b"FLAPSIM_STATE/1"
                    if command is None
                    else (b"FLAPSIM_MECH/1 " + json.dumps(command, separators=(",", ":")).encode())
                )
                stage = "model_exchange"
                result = self._exchange(request)
                stage = "reply_validation"
                if command is not None and isinstance(result, dict) and set(result) == {"error"}:
                    if result["error"] not in {
                        "module_not_found",
                        "invalid_mechanics",
                        "revision_conflict",
                        "model_busy",
                        "stale_or_reused_id",
                        "revision_exhausted",
                        "backend_unavailable",
                        "controller_off",
                    }:
                        raise ValueError("invalid model rejection")
                else:
                    self._validate_snapshot(result)
                    if (
                        command is not None
                        and result.get("last_operation", {}).get("command") != command
                    ):
                        raise ValueError("model reply does not identify the command")
                if command is not None:
                    stage = "result_audit"
                    journal({"phase": "result", "command": command, "result": result})
                if with_power:
                    return result, self.power.state if self.power else "on"
                return result
            except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
                self.failure = {
                    "stage": stage,
                    "error": type(error).__name__,
                    "detail": str(error)[:160],
                    # The failing attempt, and the earlier replies' timing, to tell a slow model
                    # from a stuck one (#104).
                    **({"elapsed_ms": round(self.elapsed * 1000, 1)} if self.elapsed else {}),
                    **(self.latency() or {}),
                }
                self.closed = True
                self.connection.close()
                if command is not None:
                    try:
                        journal(
                            {
                                "phase": "unknown",
                                "command": command,
                                "stage": stage,
                                "error": type(error).__name__,
                                "channel": "closed",
                            }
                        )
                    except (OSError, ValueError, TypeError, KeyError, RecursionError):
                        # The audit sink itself may be broken; never invent a durable outcome.
                        pass
                # An intent without a result is also unknown, never automatically retried.
                raise Unavailable("model reply unavailable") from error
        finally:
            self.lock.release()
