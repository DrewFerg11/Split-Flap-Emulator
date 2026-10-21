"""Read a complete settlement only when two activity reads confirm its freshness."""

import json
import math
from pathlib import Path

MAX_CONTROL_ID = 2**53 - 1
MECHANICS = {
    "steps_per_rotation",
    "magnet_position",
    "flap_offset",
    "sensor_window",
    "sensor_polarity",
}


def resolved_mechanics(module, drum):
    return {
        name: module.get(name, drum.get(name, default))
        for name, default in [
            ("steps_per_rotation", None),
            ("magnet_position", None),
            ("flap_offset", 0),
            ("sensor_window", None),
            ("sensor_polarity", "active-high"),
        ]
    }


def validate_mechanics(value, flap_count):
    if not isinstance(value, dict) or set(value) != MECHANICS:
        raise ValueError("missing or unknown mechanics fields")
    for key in MECHANICS - {"sensor_polarity"}:
        if type(value[key]) is not int:
            raise ValueError("mechanics require literal integers")
    steps = value["steps_per_rotation"]
    if not (
        max(4, flap_count) <= steps <= 1000000
        and 0 <= value["magnet_position"] < steps
        and 0 <= value["flap_offset"] < steps
        and 1 <= value["sensor_window"] <= steps
        and value["sensor_polarity"] in ("active-high", "active-low")
    ):
        raise ValueError("invalid resolved mechanics")
    return value


def validate_overlay(value):
    """Versioned extension shared by state and lightweight activity records."""
    if "mechanics_version" not in value:
        if any(
            k in value
            for k in ("mechanics_revision", "initial_mechanics", "last_operation", "initial_flaps")
        ):
            raise ValueError("incomplete mechanics extension")
        return 0
    if type(value["mechanics_version"]) is not int or value["mechanics_version"] != 1:
        raise ValueError("unsupported mechanics version")
    revision = value.get("mechanics_revision")
    if type(revision) is not int or not 1 <= revision <= MAX_CONTROL_ID:
        raise ValueError("invalid mechanics revision")
    if type(value.get("guest_resets")) is not int or value["guest_resets"] < 0:
        raise ValueError("invalid guest resets")
    operation = value.get("last_operation")
    if not isinstance(operation, dict) or set(operation) != {
        "command",
        "revision",
        "sequence",
        "guest_ns",
    }:
        raise ValueError("invalid mechanics operation")
    for key, lower, upper in [
        ("revision", revision, revision),
        ("sequence", 1, value["sequence"]),
        ("guest_ns", 0, value["guest_ns"]),
    ]:
        if type(operation[key]) is not int or not lower <= operation[key] <= upper:
            raise ValueError("invalid mechanics operation identity")
    command = operation["command"]
    if not isinstance(command, dict) or set(command) != {
        "id",
        "bus",
        "addr",
        "expected_revision",
        "patch",
    }:
        raise ValueError("invalid mechanics command")
    for key, lower, upper in [
        ("id", 1, MAX_CONTROL_ID),
        ("bus", 0, 1),
        ("addr", 32, 39),
        ("expected_revision", revision - 1, revision - 1),
    ]:
        if type(command[key]) is not int or not lower <= command[key] <= upper:
            raise ValueError("invalid mechanics command identity")
    if (
        not isinstance(command["patch"], dict)
        or not command["patch"]
        or set(command["patch"]) - MECHANICS
    ):
        raise ValueError("invalid mechanics command patch")
    return revision


COUNTERS = {"forward", "backward", "stalls", "writes", "reads", "anomalies"}


def validate_counters(value):
    if not isinstance(value, dict) or any(
        type(value.get(k)) is not int or value[k] < 0 for k in COUNTERS
    ):
        raise ValueError("missing or invalid model counters")


def read_settlement(out: Path, hardware):
    """None means no applicable settlement yet; malformed complete output raises ValueError."""
    try:
        before = json.loads((out / "activity.json").read_text())
        state = json.loads((out / "state.json").read_text())
        after = json.loads((out / "activity.json").read_text())
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid model JSON: {error}") from error
    for value in [before, state, after]:
        validate_identity(value)
    identity = (state["movement_id"], state["settlement_id"], state.get("mechanics_revision", 0))
    if not all(v["settled"] for v in [before, state, after]) or any(
        (v["movement_id"], v["settlement_id"], v.get("mechanics_revision", 0)) != identity
        for v in [before, after]
    ):
        return None
    return validate_state(state, hardware)


def validate_state(state, hardware, *, require_settled=True):
    """Validate physical state; moving snapshots require an explicit opt-in."""
    validate_identity(state)
    if require_settled and not state["settled"]:
        raise ValueError("historical snapshot is not settled")
    modules = state.get("modules")
    if not isinstance(modules, list) or len(modules) != len(hardware["modules"]):
        raise ValueError("model settlement has missing/extra modules")
    revision = validate_overlay(state)
    initial = [resolved_mechanics(m, hardware["drum"]) for m in hardware["modules"]]
    if revision:
        if state.get("initial_flaps") != hardware["drum"]["flaps"]:
            raise ValueError("model initial flap order does not match hardware")
        recorded_initial = state.get("initial_mechanics")
        if not isinstance(recorded_initial, list):
            raise ValueError("missing initial mechanics")
        for mechanics in recorded_initial:
            validate_mechanics(mechanics, len(hardware["drum"]["flaps"]))
        if recorded_initial != initial:
            raise ValueError("model initial mechanics do not match hardware")
    text, rows = "", ["", ""]
    for actual, expected in zip(modules, hardware["modules"], strict=True):
        if not isinstance(actual, dict) or any(
            type(actual.get(k)) is not int or actual[k] != expected[k] for k in ["bus", "addr"]
        ):
            raise ValueError("model module identity/order mismatch")
        if type(actual.get("energized")) is not bool:
            raise ValueError("invalid model energized flag")
        if require_settled and actual["energized"]:
            raise ValueError("settled module still has energized coils")
        if type(actual.get("hall")) is not bool:
            raise ValueError("invalid model hall value")
        if type(actual.get("latch")) is not int or not 0 <= actual["latch"] <= 65535:
            raise ValueError("invalid model latch")
        if type(actual.get("phase")) is not int or not -1 <= actual["phase"] <= 3:
            raise ValueError("invalid model rotor phase")
        validate_counters(actual.get("counters"))
        mechanics = initial[len(text)]
        if revision:
            mechanics = validate_mechanics(actual.get("mechanics"), len(hardware["drum"]["flaps"]))
        elif "mechanics" in actual and actual["mechanics"] != mechanics:
            raise ValueError("model mechanics do not match configured hardware")
        angle, char = actual.get("angle"), actual.get("char")
        steps = mechanics["steps_per_rotation"]
        if type(angle) is not int or not 0 <= angle < steps:
            raise ValueError("invalid model physical position")
        if not isinstance(char, str) or len(char) != 1 or char not in hardware["drum"]["flaps"]:
            raise ValueError("invalid model displayed character")
        position, error = flap_geometry(actual, mechanics, hardware["drum"])
        flaps = hardware["drum"]["flaps"]
        if char != flaps[math.floor(position + 0.5) % len(flaps)]:
            raise ValueError("model character does not match physical position")
        for key, calculated in [("flap_position", position), ("flap_error", error)]:
            if key in actual and (
                type(actual[key]) not in (int, float)
                or not math.isfinite(actual[key])
                or not math.isclose(actual[key], calculated, abs_tol=1e-9)
            ):
                raise ValueError(f"invalid model {key}")
        # Derive fields for older v1 recordings as well as validating new output.
        actual.update(flap_position=position, flap_error=error)
        if revision:
            present = ((angle - mechanics["magnet_position"]) % steps) < mechanics["sensor_window"]
            if actual["hall"] != (present != (mechanics["sensor_polarity"] == "active-low")):
                raise ValueError("model hall does not match effective mechanics")
        text += char
        rows[expected["bus"]] += char
    if state.get("text") != text or state.get("rows") != rows:
        raise ValueError("model text/row data does not match module order")
    if any(sum(m["counters"][k] for m in modules) != state["counters"][k] for k in COUNTERS):
        raise ValueError("model aggregate counters do not match module counters")
    if state["settled"] != (not any(m["energized"] for m in modules)):
        raise ValueError("model settled flag does not match coils")
    if revision:
        command = state["last_operation"]["command"]
        target = next(
            (m for m in modules if (m["bus"], m["addr"]) == (command["bus"], command["addr"])), None
        )
        if target is None or any(
            type(v) is not type(target["mechanics"].get(k)) or v != target["mechanics"][k]
            for k, v in command["patch"].items()
        ):
            raise ValueError("mechanics do not match last applied command")
    return state


def flap_geometry(module, mechanics, drum):
    steps = mechanics.get("steps_per_rotation", drum["steps_per_rotation"])
    offset = mechanics.get("flap_offset", drum.get("flap_offset", 0))
    position = ((module["angle"] - offset) % steps) * len(drum["flaps"]) / steps
    return position, position - math.floor(position + 0.5)


def display_aligned(state, expected, tolerance=0.25):
    """Wildcards ignore alignment; named flaps must land near their centers."""
    return all(
        char == "." or abs(module["flap_error"]) <= tolerance
        for char, module in zip(expected, state["modules"], strict=True)
    )


def validate_identity(value):
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError("unsupported or missing model schema_version")
    for key in ["movement_id", "settlement_id", "guest_ns", "sequence"]:
        if type(value.get(key)) is not int or value[key] < 0:
            raise ValueError(f"invalid model {key}")
    if type(value.get("settled")) is not bool:
        raise ValueError("invalid model settled flag")
    validate_counters(value.get("counters"))
    validate_overlay(value)
