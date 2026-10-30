"""Strict command schemas shared by the HTTP API and declarative scenario controls."""

import copy
import json

from flapsim.faults import validate_faults
from flapsim.state import MAX_CONTROL_ID, MECHANICS


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("non-finite JSON value")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


def validate_command(value, bus, addr):
    if not isinstance(value, dict) or set(value) != {"id", "expected_revision", "patch"}:
        raise ValueError("expected id, expected_revision and patch")
    for key, lo, hi in [("id", 1, MAX_CONTROL_ID), ("expected_revision", 0, MAX_CONTROL_ID - 1)]:
        if type(value[key]) is not int or not lo <= value[key] <= hi:
            raise ValueError("invalid " + key)
    patch = value["patch"]
    if not isinstance(patch, dict) or not patch or set(patch) - (MECHANICS | {"faults"}):
        raise ValueError("empty or unknown mechanics fields")
    for key, part in patch.items():
        if key == "faults":
            validate_faults(part, partial=True)
        elif key == "sensor_polarity":
            if part not in ("active-high", "active-low"):
                raise ValueError("invalid sensor polarity")
        elif type(part) is not int or not 0 <= part <= 1000000:
            raise ValueError("mechanics require literal integers in range")
    return {**value, "bus": bus, "addr": addr}


def validate_operation(kind, value, hardware, *, bus=None, addr=None):
    """One typed command schema for HTTP and declarative scenario callers."""
    if kind == "mechanics":
        if (
            type(bus) is not int
            or type(addr) is not int
            or (bus, addr)
            not in {(module["bus"], module["addr"]) for module in hardware["modules"]}
        ):
            raise ValueError("module_not_found")
        return validate_command(value, bus, addr)
    if kind not in {"reset", "power", "speed"}:
        raise ValueError("unknown control operation")
    from flapsim.speed import valid_speed

    if (
        not isinstance(value, dict)
        or set(value)
        != ({"id", "speed"} if kind == "speed" else {"id", "on"} if kind == "power" else {"id"})
        or type(value["id"]) is not int
        or not 1 <= value["id"] <= MAX_CONTROL_ID
        or (kind == "power" and type(value["on"]) is not bool)
        or (kind == "speed" and not valid_speed(value["speed"]))
    ):
        raise ValueError("invalid_command")
    return copy.deepcopy(value)
