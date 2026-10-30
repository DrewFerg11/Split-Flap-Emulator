"""Scenario documents: variables, step validation and native setup."""

import copy
import datetime
import json
import re

from flapsim.executor import positive_timeout
from flapsim.scenario_control import ACTIONS as CONTROL_ACTIONS

VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
MODES = ("qemu", "native")
STEPS = set(CONTROL_ACTIONS) | {
    "wait_serial",
    "expect_serial",
    "wait_display",
    "http",
    "assert_no_errors",
    "sleep_guest",
    "mqtt_publish",
    "wait_mqtt",
    "settings_stress",
}
SERVICES = {"mqtt"}
NVS_NAME = re.compile(r"[A-Za-z0-9_]{1,15}")


def reject_constant(value):
    raise ValueError(f"nonfinite JSON value: {value}")


def fields(value, required, optional=()):
    if (
        not isinstance(value, dict)
        or required - value.keys()
        or value.keys() - required - set(optional)
    ):
        raise ValueError(
            f"expected fields {sorted(required)}, optional {sorted(optional)}: {value!r}"
        )


def parse_variables(arguments):
    values = {}
    for argument in arguments:
        key, separator, value = argument.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key in values:
            raise ValueError(f"invalid or duplicate --var: {argument!r}")
        try:
            values[key] = json.loads(value, parse_constant=reject_constant)
        except json.JSONDecodeError:
            values[key] = value
    return values


def substitute(value, variables):
    if isinstance(value, str):

        def replacement(match):
            key = match[1]
            if key not in variables:
                raise ValueError(f"missing scenario variable: {key}")
            result = variables[key]
            if not isinstance(result, str):
                raise ValueError(f"typed variable {key} must occupy the whole YAML value")
            return result

        whole = VARIABLE.fullmatch(value)
        if whole:
            if whole[1] not in variables:
                raise ValueError(f"missing scenario variable: {whole[1]}")
            return copy.deepcopy(variables[whole[1]])
        resolved = VARIABLE.sub(replacement, value)
        if "${" in resolved:
            raise ValueError(f"malformed variable expression: {value!r}")
        return resolved
    if isinstance(value, list):
        return [substitute(v, variables) for v in value]
    if isinstance(value, dict):
        if any(not isinstance(k, str) or "${" in k for k in value):
            raise ValueError("scenario mapping keys must be literal strings")
        return {k: substitute(v, variables) for k, v in value.items()}
    return value


def validate_match(value):
    fields(value, {"contains"} if "contains" in value else {"regex"})
    pattern = next(iter(value.values()))
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("serial matcher must be a nonempty string")
    if "regex" in value:
        try:
            compiled = re.compile(pattern)
            if compiled.search(""):
                raise ValueError("serial regex must consume characters")
        except re.error as error:
            raise ValueError(f"invalid serial regex: {error}") from error


def validate_step(step, hardware, services=(), *, mode="qemu", runtime_faulted=()):
    if not isinstance(step, dict) or len(step.keys() & STEPS) != 1:
        raise ValueError(f"each step needs exactly one supported action: {sorted(STEPS)}")
    kind = next(iter(step.keys() & STEPS))
    fields(step, {kind}, {"timeout", "name"})
    if "timeout" in step:
        positive_timeout(step["timeout"])
    if "name" in step and (not isinstance(step["name"], str) or not step["name"]):
        raise ValueError("step name must be a nonempty string")
    value = step[kind]
    if kind in CONTROL_ACTIONS:
        from flapsim.scenario_control import commands

        commands(kind, value, hardware)
        if kind == "set_speed" and mode != "native":
            raise ValueError("set_speed requires native mode; QEMU timing is fixed at startup")
    elif kind in {"wait_serial", "expect_serial"}:
        if isinstance(value, str):
            value = {"contains": value}
            step[kind] = value
        if not isinstance(value, dict):
            raise ValueError("serial matcher must be a string or mapping")
        validate_match(value)
    elif kind == "wait_display":
        fields(value, {"text"}, {"history", "allow_unchanged"})
        text = value["text"]
        if (
            not isinstance(text, str)
            or len(text) != len(hardware["modules"])
            or any(c != "." and c not in hardware["drum"]["flaps"] for c in text)
        ):
            raise ValueError("display text must have one flap character or '.' wildcard per module")
        for key in ["history", "allow_unchanged"]:
            if key in value and type(value[key]) is not bool:
                raise ValueError(f"{key} must be boolean")
        if value.get("history") and value.get("allow_unchanged"):
            raise ValueError("historical waits cannot assert idempotent commands")
    elif kind == "http":
        fields(value, {"path", "status"}, {"method", "body", "json", "wait_ready"})
        if (
            not isinstance(value["path"], str)
            or not value["path"].startswith("/")
            or value["path"].startswith("//")
            or "\r" in value["path"]
            or "\n" in value["path"]
        ):
            raise ValueError("HTTP path must be a relative AP path starting with /")
        if type(value["status"]) is not int or not 100 <= value["status"] <= 599:
            raise ValueError("HTTP status must be an integer in 100–599")
        if value.get("method", "GET") not in {"GET", "POST"}:
            raise ValueError("HTTP method must be GET or POST")
        if "body" in value and value.get("method", "GET") != "POST":
            raise ValueError("HTTP body requires POST")
        if "wait_ready" in value and type(value["wait_ready"]) is not bool:
            raise ValueError("wait_ready must be boolean")
        if value.get("wait_ready") and value.get("method", "GET") != "GET":
            raise ValueError("only read-only GET readiness checks may retry")
    elif kind == "settings_stress":
        fields(value, {"rounds"}, {"readers"})
        for key, low, high in [("rounds", 1, 500), ("readers", 0, 16)]:
            if key in value and (type(value[key]) is not int or not low <= value[key] <= high):
                raise ValueError(f"settings_stress {key} must be an integer in {low}–{high}")
    elif kind == "assert_no_errors":
        fields(value, set(), {"fresh_nvs", "default_missing_bus1", "faulted_modules"})
        if any(type(v) is not bool for k, v in value.items() if k != "faulted_modules"):
            raise ValueError("error policy flags must be boolean")
        if "faulted_modules" in value:
            # Firmware module numbers are I2C addresses, so only modules configured with an
            # injected I2C fault (#19) may have their write failures excused.
            faulted = {
                m["addr"] for m in hardware["modules"] if m.get("faults", {}).get("i2c")
            } | set(runtime_faulted)
            modules = value["faulted_modules"]
            if (
                not isinstance(modules, list)
                or not modules
                or any(type(m) is not int for m in modules)
                or len(set(modules)) != len(modules)
                or not set(modules) <= faulted
            ):
                raise ValueError(
                    "faulted_modules must list distinct addresses of modules with an injected "
                    f"I2C fault ({sorted(faulted)})"
                )
            if value.get("default_missing_bus1"):
                raise ValueError("faulted_modules cannot be combined with default_missing_bus1")
        if value.get("default_missing_bus1") and any(m["bus"] == 1 for m in hardware["modules"]):
            raise ValueError(
                "default missing-bus allowance applies only to eight physical bus-0 modules"
            )
        if value.get("default_missing_bus1") and [m["addr"] for m in hardware["modules"]] != list(
            range(32, 40)
        ):
            raise ValueError("missing-bus allowance requires the eight default bus-0 addresses")
    elif kind in {"mqtt_publish", "wait_mqtt"}:
        if "mqtt" not in services:
            raise ValueError(f"{kind} requires the scenario to declare services: [mqtt]")
        if kind == "mqtt_publish":
            fields(value, {"topic", "payload"}, {"retain"})
            if not isinstance(value["payload"], str):
                raise ValueError("MQTT payload must be a string")
            if "retain" in value and type(value["retain"]) is not bool:
                raise ValueError("retain must be boolean")
        else:
            fields(value, {"topic"}, {"payload", "json", "retained"})
            if "payload" in value and "json" in value:
                raise ValueError("wait_mqtt matches either payload or json, not both")
            if "payload" in value and not isinstance(value["payload"], str):
                raise ValueError("MQTT payload must be a string")
            if "retained" in value and type(value["retained"]) is not bool:
                raise ValueError("retained must be boolean")
        topic = value["topic"]
        if not isinstance(topic, str) or not topic or any(c in topic for c in "#+\0"):
            raise ValueError("MQTT topic must be a nonempty name without wildcards")
    else:
        positive_timeout(value)


def native_setup(document):
    """Native-only device preparation: services, a seeded settings store and the wall clock."""
    setup = {"services": [], "nvs": {}, "clock": None}
    if not {"services", "nvs", "clock"} & document.keys():
        return setup
    if document.get("modes") != ["native"]:
        raise ValueError("services, nvs and clock require modes: [native]")
    services = document.get("services", [])
    if (
        not isinstance(services, list)
        or len(set(services)) != len(services)
        or any(s not in SERVICES for s in services)
    ):
        raise ValueError(f"services must be a list drawn from {sorted(SERVICES)}")
    setup["services"] = services
    nvs = document.get("nvs", {})
    if not isinstance(nvs, dict):
        raise ValueError("nvs must map namespaces to settings")
    for space, values in nvs.items():
        if (
            not isinstance(space, str)
            or not NVS_NAME.fullmatch(space)
            or not isinstance(values, dict)
        ):
            raise ValueError("nvs namespaces must be 1-15 character names mapping to settings")
        for key, value in values.items():
            if not isinstance(key, str) or not NVS_NAME.fullmatch(key):
                raise ValueError(f"nvs key {key!r} must be a 1-15 character name")
            if type(value) is int:
                if not -(2**31) <= value < 2**31:
                    raise ValueError(f"nvs integer {key} must fit 32 bits")
            elif type(value) is not str and type(value) is not float:
                raise ValueError(f"nvs value {key} must be a string, integer or float")
    setup["nvs"] = nvs
    if "clock" in document:
        clock = document["clock"]
        fields(clock, {"start"}, {"sync"})
        try:
            start = datetime.datetime.fromisoformat(clock["start"])
        except (TypeError, ValueError) as error:
            raise ValueError("clock.start must be an ISO 8601 UTC time") from error
        if start.utcoffset() != datetime.timedelta(0):
            raise ValueError("clock.start must be in UTC (end with Z)")
        if clock.get("sync", "boot") not in {"boot", "station"}:
            raise ValueError("clock.sync must be boot or station")
        setup["clock"] = {
            "mode": "fixed",
            "start": int(start.timestamp()),
            "sync": clock.get("sync", "boot"),
        }
    return setup
