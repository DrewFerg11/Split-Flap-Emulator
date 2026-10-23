"""Canonical #19 fault names, deterministic controls, and versioned resume evidence."""

DEFAULT_FAULTS = {"sensor": "ok", "motor": "ok", "i2c": "ok"}
FAULT_SUPPORT = {
    "sensor": ["ok", "stuck_active", "stuck_inactive"],
    "motor": ["ok", "dead"],
    "i2c": ["ok", "missing"],
}
KINDS = {
    "sensor": {*FAULT_SUPPORT["sensor"], "inverted", "noisy"},
    "motor": {*FAULT_SUPPORT["motor"], "slip"},
    "i2c": {*FAULT_SUPPORT["i2c"], "disappear", "nack", "corrupt"},
}
PARAMETERS = {"noisy": "sensor_noise", "slip": "slip", "nack": "nack", "corrupt": "corrupt"}


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
        if type(value[key]) is not str or value[key] not in choices:
            raise ValueError("invalid fault mode")
    if type(value["seed"]) is not int or not 0 <= value["seed"] <= 0xFFFFFFFF:
        raise ValueError("invalid fault seed")
    if type(value["configured"]) is not bool:
        raise ValueError("invalid configured fault flag")
    wanted = {PARAMETERS[v] for v in (value[k] for k in KINDS) if v in PARAMETERS}
    if value["i2c"] == "disappear":
        wanted.add("disappear_at_ns")
    if set(value) != {*DEFAULT_FAULTS, "seed", "configured", *wanted}:
        raise ValueError("unknown or missing fault parameters")
    for key in wanted:
        number = value[key]
        if key == "disappear_at_ns":
            valid = type(number) is int and 0 < number < 2**63
        else:
            valid = type(number) in (int, float) and 0 < number <= 1 and value["seed"] != 0
        if not valid:
            raise ValueError("invalid fault parameter")
    if not value["configured"] and (value["seed"] or any(value[k] != "ok" for k in KINDS)):
        raise ValueError("unconfigured module has faults")
    return value


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
        mode = effective[key]
        if mode != initial[key] and mode not in FAULT_SUPPORT[key]:
            raise ValueError("unsupported runtime fault mode")
        parameter = "disappear_at_ns" if mode == "disappear" else PARAMETERS.get(mode)
        if parameter and effective[parameter] != initial.get(parameter):
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
                **{k: config[k] for k in KINDS if config[k] != "ok"},
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
