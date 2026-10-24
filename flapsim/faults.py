"""Canonical #19/#64 fault names, deterministic controls, and versioned resume evidence."""

# The pre-#64 kinds. `power` (#64) is optional everywhere: a healthy rail is simply absent, so
# evidence from runs that never use it is byte-identical to what older versions wrote.
DEFAULT_FAULTS = {"sensor": "ok", "motor": "ok", "i2c": "ok"}
FAULT_SUPPORT = {
    "sensor": ["ok", "stuck_active", "stuck_inactive"],
    "motor": ["ok", "dead"],
    "i2c": ["ok", "missing"],
    "power": ["ok", "lost"],
}
KINDS = {
    "sensor": {*FAULT_SUPPORT["sensor"], "inverted", "noisy"},
    "motor": {*FAULT_SUPPORT["motor"], "slip"},
    "i2c": {*FAULT_SUPPORT["i2c"], "disappear", "nack", "corrupt"},
    "power": {*FAULT_SUPPORT["power"], "drop", "brownout", "sag"},
}
PARAMETERS = {
    "noisy": "sensor_noise",
    "slip": "slip",
    "nack": "nack",
    "corrupt": "corrupt",
    "brownout": "brownout",
    "sag": "sag",
}
# Modes whose parameters are guest-time instants in integer nanoseconds.
INSTANTS = {
    "disappear": ("disappear_at_ns",),
    "drop": ("power_off_at_ns", "power_on_at_ns"),
}


def parameters(mode):
    """Every normalized parameter a mode requires."""
    return INSTANTS.get(mode) or ((PARAMETERS[mode],) if mode in PARAMETERS else ())


def validate_faults(value, *, partial=False):
    if not isinstance(value, dict) or not value:
        raise ValueError("missing or unknown fault fields")
    if partial:
        if set(value) - FAULT_SUPPORT.keys() or any(
            type(part) is not str or part not in FAULT_SUPPORT[key] for key, part in value.items()
        ):
            raise ValueError("invalid deterministic fault")
        return value
    if not {*DEFAULT_FAULTS, "seed", "configured"} <= value.keys():
        raise ValueError("incomplete fault state")
    for key, choices in KINDS.items():
        # An absent kind is healthy; an explicit healthy rail is not the canonical form.
        if key not in value and key not in DEFAULT_FAULTS:
            continue
        if type(value[key]) is not str or value[key] not in choices:
            raise ValueError("invalid fault mode")
        if key not in DEFAULT_FAULTS and value[key] == "ok":
            raise ValueError("healthy power must be omitted")
    if type(value["seed"]) is not int or not 0 <= value["seed"] <= 0xFFFFFFFF:
        raise ValueError("invalid fault seed")
    if type(value["configured"]) is not bool:
        raise ValueError("invalid configured fault flag")
    modes = {key: value.get(key, "ok") for key in KINDS}
    wanted = {name for mode in modes.values() for name in parameters(mode)}
    optional = {"power"} & value.keys()
    if set(value) != {*DEFAULT_FAULTS, "seed", "configured", *optional, *wanted}:
        raise ValueError("unknown or missing fault parameters")
    for key in wanted:
        number = value[key]
        if key.endswith("_ns"):
            valid = type(number) is int and 0 < number < 2**63
        else:
            valid = type(number) in (int, float) and 0 < number <= 1 and value["seed"] != 0
        if not valid:
            raise ValueError("invalid fault parameter")
    if modes["power"] == "drop" and value["power_on_at_ns"] <= value["power_off_at_ns"]:
        raise ValueError("power must come back after it drops")
    if not value["configured"] and (value["seed"] or any(mode != "ok" for mode in modes.values())):
        raise ValueError("unconfigured module has faults")
    return value


def power_evidence(faults, effects):
    """Mirrors the model: evidence appears once the rail is faulted, was cut, or is down."""
    return "power" in faults or bool(effects.get("power_losses")) or effects.get("powered") is False


def validate_fault_counters(effects, faults):
    """Observed effects. Power evidence (#64) exists exactly once the rail is or was faulted."""
    base = {"blocked_steps", "nacks", "rng", "events"}
    if not isinstance(effects, dict):
        raise ValueError("invalid observed fault counters")
    extended = "power_losses" in effects or "powered" in effects
    numbers = {k: v for k, v in effects.items() if k != "powered"}
    if (
        set(effects) != base | ({"power_losses", "powered"} if extended else set())
        or any(type(n) is not int or not 0 <= n < 2**63 for n in numbers.values())
        or (extended and type(effects["powered"]) is not bool)
        or extended != power_evidence(faults, effects)
        or effects["rng"] >= 2**32
        or bool(effects["rng"]) != bool(faults["seed"])
        or effects["events"] < effects["blocked_steps"] + effects["nacks"]
    ):
        raise ValueError("invalid observed fault counters")


def resolved_faults(module):
    configured = "faults" in module
    value = {**DEFAULT_FAULTS, "seed": 0, "configured": configured, **module.get("faults", {})}
    return validate_faults(value)


def validate_effective_faults(initial, effective):
    if effective["seed"] != initial["seed"] or (
        initial["configured"] and not effective["configured"]
    ):
        raise ValueError("fault seed/configuration binding changed")
    for key in KINDS:
        mode = effective.get(key, "ok")
        if mode != initial.get(key, "ok") and mode not in FAULT_SUPPORT[key]:
            raise ValueError("unsupported runtime fault mode")
        for parameter in parameters(mode):
            if effective[parameter] != initial.get(parameter):
                raise ValueError("startup fault parameter changed")


def validate_summary(state, configurations, *, versioned):
    expected = []
    actual = state.get("faults", [])
    if not isinstance(actual, list) or any(
        not isinstance(entry, dict)
        or any(type(entry.get(key)) is not int for key in ("bus", "addr", "events"))
        or not 0 <= entry["events"] < 2**63
        for entry in actual
    ):
        raise ValueError("invalid fault summary")
    for module, config in zip(state["modules"], configurations, strict=True):
        if not config["configured"]:
            continue
        if versioned:
            events = module["fault_counters"]["events"]
        else:
            # #19's unversioned evidence carried mode/effects summaries only.
            entry = actual[len(expected)] if len(actual) > len(expected) else {}
            events = entry.get("events") if isinstance(entry, dict) else None
            if type(events) is not int or not 0 <= events < 2**63:
                raise ValueError("invalid legacy fault effects")
        expected.append(
            {
                **{k: config[k] for k in KINDS if config.get(k, "ok") != "ok"},
                "events": events,
                "bus": module["bus"],
                "addr": module["addr"],
            }
        )
    if actual != expected or (not expected and "faults" in state):
        raise ValueError("fault summary does not match module faults")


def validate_fault_identity(value):
    if "faults_version" not in value:
        if "initial_faults" in value:
            raise ValueError("incomplete faults extension")
        return False
    if type(value["faults_version"]) is not int or value["faults_version"] != 1:
        raise ValueError("unsupported faults version")
    initial = value.get("initial_faults")
    if not isinstance(initial, list) or not initial:
        raise ValueError("missing initial faults")
    for entry in initial:
        validate_faults(entry)
    return True
