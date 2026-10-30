"""Pure checks scenarios apply to serial text, display text and JSON."""

import re

from flapsim.executor import ExecutionFailed


def serial_match(matcher, text):
    if "contains" in matcher:
        start = text.find(matcher["contains"])
        return None if start < 0 else (start, start + len(matcher["contains"]))
    match = re.search(matcher["regex"], text, re.MULTILINE)
    return (match.start(), match.end()) if match else None


def display_matches(expected, actual):
    return len(expected) == len(actual) and all(
        a == "." or a == b for a, b in zip(expected, actual, strict=True)
    )


def json_subset(expected, actual, path="$"):
    if type(expected) is not type(actual):
        raise ExecutionFailed(
            f"JSON type mismatch at {path}: expected {expected!r}, got {actual!r}"
        )
    if isinstance(expected, dict):
        for key, value in expected.items():
            if key not in actual:
                raise ExecutionFailed(f"missing JSON key {path}.{key}")
            json_subset(value, actual[key], f"{path}.{key}")
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            raise ExecutionFailed(f"JSON array length mismatch at {path}")
        for index, (a, b) in enumerate(zip(expected, actual, strict=True)):
            json_subset(a, b, f"{path}[{index}]")
    elif expected != actual:
        raise ExecutionFailed(f"JSON mismatch at {path}: expected {expected!r}, got {actual!r}")


def serial_errors(text, policy, known=None, observed=None):
    """Fail on firmware error lines, except explicit policy flags and baseline-tolerated lines."""
    missing = []
    # Reset can interrupt the source header as well as the UART payload.
    # Only an observed prefix of the exact known diagnostic followed by the
    # exact ROM banner qualifies; unknown characters and suffixes still fail.
    stamp = r"\[\s*\d+\]"
    diagnostic = "[E][Preferences.cpp:50] begin(): nvs_open failed: NOT_FOUND"
    complete = re.compile(stamp + re.escape(diagnostic))
    interrupted = re.compile(stamp + r"(.*)ets Jul 29 2019 12:21:46")
    for line in text.splitlines():
        if policy.get("fresh_nvs") and (
            complete.fullmatch(line)
            or ((match := interrupted.fullmatch(line)) and diagnostic.startswith(match[1]))
        ):
            continue
        match = re.fullmatch(r"Error writing data to module (\d+), error code: 2", line)
        if match and int(match[1]) in policy.get("faulted_modules", ()):
            continue
        if policy.get("default_missing_bus1") and match:
            missing.append(int(match[1]))
        elif known and (entry := known.tolerated_serial(line)):
            if observed is not None:
                record = observed.setdefault(
                    entry["regex"], {"issue": entry["issue"], "count": 0, "first": line}
                )
                record["count"] += 1
        elif re.search(r"\[E\]|\b(error|failed|panic|abort|exception)\b", line, re.IGNORECASE):
            raise ExecutionFailed(f"unexpected serial error: {line}")
    if policy.get("default_missing_bus1") and sorted(missing) != list(range(32, 40)):
        raise ExecutionFailed(
            f"expected one default-bus write failure per absent module; got {missing}"
        )
