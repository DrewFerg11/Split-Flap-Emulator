"""Read a complete settlement only when two activity reads confirm its freshness."""

import json
from pathlib import Path

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
    identity = (state["movement_id"], state["settlement_id"])
    if not all(v["settled"] for v in [before, state, after]) or any(
        (v["movement_id"], v["settlement_id"]) != identity for v in [before, after]
    ):
        return None
    return validate_state(state, hardware)


def validate_state(state, hardware):
    """Validate a complete, all-off historical settlement without reading live activity."""
    validate_identity(state)
    if not state["settled"]:
        raise ValueError("historical snapshot is not settled")
    modules = state.get("modules")
    if not isinstance(modules, list) or len(modules) != len(hardware["modules"]):
        raise ValueError("model settlement has missing/extra modules")
    text, rows = "", ["", ""]
    for actual, expected in zip(modules, hardware["modules"], strict=True):
        if not isinstance(actual, dict) or any(
            type(actual.get(k)) is not int or actual[k] != expected[k] for k in ["bus", "addr"]
        ):
            raise ValueError("model module identity/order mismatch")
        if actual.get("energized") is not False:
            raise ValueError("settled module still has energized coils")
        if type(actual.get("hall")) is not bool:
            raise ValueError("invalid model hall value")
        if type(actual.get("latch")) is not int or not 0 <= actual["latch"] <= 65535:
            raise ValueError("invalid model latch")
        if type(actual.get("phase")) is not int or not -1 <= actual["phase"] <= 3:
            raise ValueError("invalid model rotor phase")
        validate_counters(actual.get("counters"))
        angle, char = actual.get("angle"), actual.get("char")
        if type(angle) is not int or not 0 <= angle < hardware["drum"]["steps_per_rotation"]:
            raise ValueError("invalid model physical position")
        if not isinstance(char, str) or len(char) != 1 or char not in hardware["drum"]["flaps"]:
            raise ValueError("invalid model displayed character")
        text += char
        rows[expected["bus"]] += char
    if state.get("text") != text or state.get("rows") != rows:
        raise ValueError("model text/row data does not match module order")
    if any(sum(m["counters"][k] for m in modules) != state["counters"][k] for k in COUNTERS):
        raise ValueError("model aggregate counters do not match module counters")
    return state


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
