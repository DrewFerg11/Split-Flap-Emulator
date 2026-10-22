"""Checksum-bound known-issue baselines: linked, narrow and reported, never silent."""

import re
from pathlib import Path

import yaml

from flapsim.hardware import HardwareLoader

ISSUE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*")
SHA256 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
ACTIONS = {
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


def _fields(value, where, required, optional=()):
    if (
        not isinstance(value, dict)
        or required - value.keys()
        or value.keys() - required - set(optional)
    ):
        raise ValueError(
            f"{where}: expected fields {sorted(required)}, optional {sorted(optional)}: {value!r}"
        )


def _text(value, where):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} must be a nonempty string")
    return value


def _regex(value, where):
    _text(value, where)
    if not value.startswith("^") or not value.endswith("$"):
        raise ValueError(f"{where} must be anchored with ^ and $")
    try:
        compiled = re.compile(value)
    except re.error as error:
        raise ValueError(f"{where} is not a valid regex: {error}") from error
    if compiled.fullmatch(""):
        raise ValueError(f"{where} must not match an empty line")
    return compiled


def _issue(entry, where):
    issue = _text(entry["issue"], f"{where}.issue")
    if not ISSUE.fullmatch(issue):
        raise ValueError(f"{where}.issue must look like owner/repo#123: {issue!r}")
    _text(entry["reason"], f"{where}.reason")
    return issue


class KnownIssues:
    """A validated baseline. Applies only to the firmware it names: a QEMU-mode image by its
    SHA-256, or a native-mode build by its clean firmware source commit."""

    def __init__(self, document, source="known issues"):
        binding = (
            "firmware_source_commit" if "firmware_source_commit" in document else "firmware_sha256"
        )
        _fields(
            document,
            source,
            {"version", binding, "description"},
            {"serial", "expected_failures"},
        )
        if type(document["version"]) is not int or document["version"] != 1:
            raise ValueError(f"{source}: version must be 1")
        pattern = COMMIT if binding == "firmware_source_commit" else SHA256
        if not isinstance(document[binding], str) or not pattern.fullmatch(document[binding]):
            kind = "commit SHA" if binding == "firmware_source_commit" else "SHA-256"
            raise ValueError(f"{source}: {binding} must be a full lowercase {kind}")
        _text(document["description"], f"{source}.description")
        self.source = source
        self.firmware_sha256 = document.get("firmware_sha256")
        self.firmware_source_commit = document.get("firmware_source_commit")
        self.serial = []
        for index, entry in enumerate(document.get("serial") or []):
            where = f"{source}.serial[{index}]"
            _fields(entry, where, {"issue", "reason", "regex"}, {"explains_stalls"})
            explains = entry.get("explains_stalls", 0)
            if type(explains) is not int or explains < 0:
                raise ValueError(f"{where}.explains_stalls must be a non-negative integer")
            self.serial.append(
                {
                    "issue": _issue(entry, where),
                    "reason": entry["reason"],
                    "regex": entry["regex"],
                    "explains_stalls": explains,
                    "compiled": _regex(entry["regex"], f"{where}.regex"),
                }
            )
        self.failures = []
        for index, entry in enumerate(document.get("expected_failures") or []):
            where = f"{source}.expected_failures[{index}]"
            _fields(entry, where, {"issue", "reason", "scenario", "step", "action", "error"})
            if type(entry["step"]) is not int or entry["step"] < 1:
                raise ValueError(f"{where}.step must be a positive integer")
            if entry["action"] not in ACTIONS:
                raise ValueError(f"{where}.action must be one of {sorted(ACTIONS)}")
            error = entry["error"]
            if error == "timeout":
                compiled = None
            else:
                _fields(error, f"{where}.error", {"regex"})
                compiled = _regex(error["regex"], f"{where}.error.regex")
            self.failures.append(
                {
                    "issue": _issue(entry, where),
                    "reason": entry["reason"],
                    "scenario": _text(entry["scenario"], f"{where}.scenario"),
                    "step": entry["step"],
                    "action": entry["action"],
                    "error": "timeout" if compiled is None else {"regex": error["regex"]},
                    "compiled": compiled,
                }
            )
        if not self.serial and not self.failures:
            raise ValueError(f"{source}: an empty baseline should be deleted, not loaded")

    def check_firmware(self, sha256):
        if self.firmware_sha256 is None:
            raise ValueError(f"{self.source} binds native firmware source, not a QEMU image")
        if sha256 != self.firmware_sha256:
            raise ValueError(
                f"{self.source} applies only to firmware {self.firmware_sha256}; got {sha256}"
            )

    def check_source(self, commit, dirty):
        """Native mode: only an unmodified checkout of the named commit qualifies."""
        if self.firmware_source_commit is None:
            raise ValueError(f"{self.source} binds a QEMU firmware image, not native source")
        if commit != self.firmware_source_commit or dirty is not False:
            state = "dirty " if dirty else ""
            raise ValueError(
                f"{self.source} applies only to clean firmware source "
                f"{self.firmware_source_commit}; got {state}{commit}"
            )

    def excused_stalls(self, serial):
        """Drum stalls explained by known serial lines: each matching line excuses at most its
        entry's explains_stalls. Returns the allowance and the issues behind it."""
        allowance, issues = 0, set()
        for line in serial.splitlines():
            entry = self.tolerated_serial(line)
            if entry and entry["explains_stalls"]:
                allowance += entry["explains_stalls"]
                issues.add(entry["issue"])
        return allowance, sorted(issues)

    def tolerated_serial(self, line):
        for entry in self.serial:
            if entry["compiled"].fullmatch(line):
                return entry
        return None

    def expected_failure(self, scenario, step, action, error, *, step_timed_out):
        for entry in self.failures:
            if (entry["scenario"], entry["step"], entry["action"]) != (scenario, step, action):
                continue
            if entry["compiled"] is None:
                if step_timed_out:
                    return entry
            elif not step_timed_out and entry["compiled"].fullmatch(error):
                return entry
        return None

    def describe(self):
        """JSON-safe description for manifests and reports."""
        return {
            "source": self.source,
            "firmware_sha256": self.firmware_sha256,
            "firmware_source_commit": self.firmware_source_commit,
            "serial": [{k: v for k, v in e.items() if k != "compiled"} for e in self.serial],
            "expected_failures": [
                {k: v for k, v in e.items() if k != "compiled"} for e in self.failures
            ],
        }


def load_known_issues(path):
    try:
        document = yaml.load(Path(path).read_text(), Loader=HardwareLoader)
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"cannot load known issues {path}: {error}") from error
    return KnownIssues(document, str(path))
