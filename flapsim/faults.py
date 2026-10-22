"""Literal deterministic fault registry shared by configuration, controls and readers."""

DEFAULT_FAULTS = {"hall": "normal", "motor": "normal", "present": True}
FAULT_SUPPORT = {
    "hall": ["normal", "stuck_active", "stuck_inactive"],
    "motor": ["normal", "dead"],
    "present": [True, False],
}


def validate_faults(value, *, partial=False):
    if not isinstance(value, dict) or not value or set(value) - DEFAULT_FAULTS.keys():
        raise ValueError("missing or unknown fault fields")
    if not partial and set(value) != DEFAULT_FAULTS.keys():
        raise ValueError("incomplete fault state")
    for key, part in value.items():
        if key == "present":
            valid = type(part) is bool
        else:
            valid = type(part) is str and part in FAULT_SUPPORT[key]
        if not valid:
            raise ValueError("invalid deterministic fault: " + key)
    return value


def resolved_faults(module):
    return (
        {**DEFAULT_FAULTS, **validate_faults(module["faults"], partial=True)}
        if "faults" in module
        else dict(DEFAULT_FAULTS)
    )


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
