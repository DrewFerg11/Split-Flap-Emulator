"""Declarative controls reuse the public API dispatcher and its durable receipts."""

import math
import time

from flapsim.control_channel import Unavailable
from flapsim.control_commands import validate_operation

ACTIONS = frozenset(
    {"set_mechanics", "set_fault", "reset", "set_power", "power_cycle", "set_speed"}
)


def commands(kind, value, hardware):
    # Import lazily to keep the basic scenario parser independent of API extras.
    from flapsim.scenario_schema import fields

    if kind in {"set_mechanics", "set_fault"}:
        field = "patch" if kind == "set_mechanics" else "faults"
        fields(value, {"id", "expected_revision", "bus", "addr", field})
        command = {k: value[k] for k in ["id", "expected_revision"]}
        command["patch"] = value[field] if kind == "set_mechanics" else {"faults": value[field]}
        target = {"bus": value["bus"], "addr": value["addr"]}
        validate_operation("mechanics", command, hardware, **target)
        return [("mechanics", command, target)]
    if kind == "power_cycle":
        fields(value, {"off_id", "on_id"}, {"off_seconds"})
        off = {"id": value["off_id"], "on": False}
        on = {"id": value["on_id"], "on": True}
        validate_operation("power", off, hardware)
        validate_operation("power", on, hardware)
        delay = value.get("off_seconds", 0)
        if (
            type(delay) not in {int, float}
            or not math.isfinite(delay)
            or not 0 <= delay <= 30
            or on["id"] <= off["id"]
        ):
            raise ValueError("power_cycle requires increasing IDs and off_seconds in [0, 30]")
        return [("power", off, {}), ("power", on, {})]
    operation = {"reset": "reset", "set_power": "power", "set_speed": "speed"}[kind]
    validate_operation(operation, value, hardware)
    return [(operation, value, {})]


def execute(context, kind, value, result):
    if context.control is None:
        raise RuntimeError("scenario control dispatcher is unavailable")
    result["actual"] = {"operations": [], "busy_rejections": 0}
    operations = commands(kind, value, context.hardware)
    for index, (operation, command, target) in enumerate(operations):
        if index and kind == "power_cycle":
            until = time.monotonic() + value.get("off_seconds", 0)
            while time.monotonic() < until:
                context.pause()
        while True:
            context.checkpoint()
            try:
                receipt = context.control.dispatch(operation, command, **target)
            except Unavailable as error:
                result["actual"]["operations"].append(
                    {"kind": operation, "command": command, "status": "unknown"}
                )
                raise RuntimeError("control result unknown; command was not retried") from error
            if receipt.get("error") in {"model_busy", "backend_busy"}:
                # Both are explicit rejections before admission/application. Never
                # retry timeouts, poisoned channels, unknown receipts or conflicts.
                result["actual"]["busy_rejections"] += 1
                context.pause()
                continue
            if "error" in receipt:
                result["actual"]["operations"].append(
                    {"kind": operation, "command": command, **receipt}
                )
                raise RuntimeError("control rejected: " + receipt["error"])
            if operation == "mechanics":
                receipt = {"status": "applied", "operation": receipt["last_operation"]}
            result["actual"]["operations"].append({"kind": operation, **receipt})
            if receipt.get("status") != "applied":
                raise RuntimeError("control result unknown; command was not retried")
            context.checkpoint()
            break
