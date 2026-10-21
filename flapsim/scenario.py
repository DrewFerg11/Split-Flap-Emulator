"""Validated, deadline-bound black-box scenarios for the real firmware."""

import copy
import http.client
import json
import re
import socket
import threading
import time
from urllib.parse import urlsplit

from flapsim.executor import ExecutionFailed, RunContext, positive_timeout
from flapsim.state import display_aligned, read_settlement, validate_identity, validate_state

VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
STEPS = {"wait_serial", "expect_serial", "wait_display", "http", "assert_no_errors", "sleep_guest"}


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


def validate_step(step, hardware):
    if not isinstance(step, dict) or len(step.keys() & STEPS) != 1:
        raise ValueError(f"each step needs exactly one supported action: {sorted(STEPS)}")
    kind = next(iter(step.keys() & STEPS))
    fields(step, {kind}, {"timeout", "name"})
    if "timeout" in step:
        positive_timeout(step["timeout"])
    if "name" in step and (not isinstance(step["name"], str) or not step["name"]):
        raise ValueError("step name must be a nonempty string")
    value = step[kind]
    if kind in {"wait_serial", "expect_serial"}:
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
    elif kind == "assert_no_errors":
        fields(value, set(), {"fresh_nvs", "default_missing_bus1"})
        if any(type(v) is not bool for v in value.values()):
            raise ValueError("error policy flags must be boolean")
        if value.get("default_missing_bus1") and any(m["bus"] == 1 for m in hardware["modules"]):
            raise ValueError(
                "default missing-bus allowance applies only to eight physical bus-0 modules"
            )
        if value.get("default_missing_bus1") and [m["addr"] for m in hardware["modules"]] != list(
            range(32, 40)
        ):
            raise ValueError("missing-bus allowance requires the eight default bus-0 addresses")
    else:
        positive_timeout(value)


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
    for line in text.splitlines():
        if policy.get("fresh_nvs") and re.fullmatch(
            r"\[\s*\d+\]\[E\]\[Preferences.cpp:50\] begin\(\): nvs_open failed: NOT_FOUND", line
        ):
            continue
        match = re.fullmatch(r"Error writing data to module (\d+), error code: 2", line)
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


def http_request(context, method, path, data):
    """Supervise the whole HTTP exchange, including trickled headers/body and cancellation."""
    address = urlsplit(context.endpoint)
    connection = http.client.HTTPConnection(address.hostname, address.port)
    context.checkpoint()
    transport = socket.create_connection(
        (address.hostname, address.port),
        timeout=min(5, max(0.001, context.deadline - time.monotonic())),
    )
    transport.settimeout(max(0.001, context.deadline - time.monotonic()))
    connection.sock = transport
    results = []

    def exchange():
        try:
            connection.request(method, path, data, {"Content-Type": "application/json"})
            with connection.getresponse() as response:
                body = response.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024:
                    raise ExecutionFailed("HTTP response exceeds 1 MiB")
                results.append({"status": response.status, "body": body.decode(errors="replace")})
        except Exception as error:
            results.append(error)

    worker = threading.Thread(target=exchange, daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            context.checkpoint()
            worker.join(0.05)
        context.checkpoint()
        if isinstance(results[0], Exception):
            raise results[0]
        return results[0]
    finally:
        try:
            transport.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        transport.close()
        connection.close()
        worker.join(1)
        if worker.is_alive():
            raise RuntimeError("HTTP worker did not stop after socket shutdown")


class ScenarioExecutor:
    def __init__(
        self,
        document,
        hardware,
        variables,
        *,
        firmware_sha256=None,
        known_issues=None,
    ):
        if known_issues is not None:
            known_issues.check_firmware(firmware_sha256)
        self.known_issues = known_issues
        self.known_serial = {}
        self.known_failure = None
        requested = parse_variables(variables)
        self.document = substitute(document, copy.deepcopy(requested))
        fields(self.document, {"version", "name", "timeout", "steps"})
        if type(self.document["version"]) is not int or self.document["version"] != 1:
            raise ValueError("scenario version must be 1")
        if not isinstance(self.document["name"], str) or not self.document["name"]:
            raise ValueError("scenario name must be a nonempty string")
        self.timeout = positive_timeout(self.document["timeout"])
        if not isinstance(self.document["steps"], list) or not self.document["steps"]:
            raise ValueError("scenario steps must be a nonempty list")
        for step in self.document["steps"]:
            validate_step(step, hardware)
        try:
            json.dumps(self.document, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError(f"scenario values must be finite JSON values: {error}") from error
        self.variables = requested
        self.results = [
            {
                "index": index + 1,
                "name": step.get("name", next(iter(step.keys() & STEPS))),
                "action": next(iter(step.keys() & STEPS)),
                "status": "not_run",
                "expected": step[next(iter(step.keys() & STEPS))],
            }
            for index, step in enumerate(self.document["steps"])
        ]
        self.serial_cursor = self.error_cursor = self.timeline_offset = 0
        self.display_cursor = 0
        self.command_baseline = None
        self.last_error_policy = None

    def execute(self, context):
        for step, result in zip(self.document["steps"], self.results, strict=True):
            started = time.monotonic()
            deadline = min(context.deadline, started + step.get("timeout", 30))
            step_context = RunContext(
                context.out,
                context.hardware,
                context.endpoint,
                context.process,
                deadline,
                context.interrupt,
            )
            result.update(status="running", guest_start_ns=self.guest_ns(context.out))
            try:
                self.action(step_context, result["action"], step[result["action"]], result)
                step_context.checkpoint()
                result["status"] = "passed"
            except BaseException as error:
                result.update(status="failed", error=str(error) or type(error).__name__)
                entry = self.expected_failure(error, result, deadline, context.deadline)
                if entry is None:
                    raise
                result.update(status="xfail", known_issue=entry["issue"])
                self.known_failure = {
                    "issue": entry["issue"],
                    "reason": entry["reason"],
                    "step": result["index"],
                    "action": result["action"],
                    "error": result["error"],
                }
                for later in self.results[result["index"] :]:
                    later["status"] = "skipped_known_issue"
                break
            finally:
                result.update(
                    wall_seconds=round(time.monotonic() - started, 3),
                    guest_end_ns=self.guest_ns(context.out),
                )
        return {
            "steps": self.results,
            "known_issues": self.known_issue_summary(),
        }

    def expected_failure(self, error, result, step_deadline, run_deadline):
        """Only assertion failures and the step's own timeout can match a baseline entry."""
        if self.known_issues is None or not isinstance(error, (ExecutionFailed, TimeoutError)):
            return None
        if result.get("alignment_failed"):
            # A lost-command timeout must not excuse an observed partial landing.
            return None
        step_timed_out = (
            isinstance(error, TimeoutError)
            and step_deadline < run_deadline
            and time.monotonic() >= step_deadline
        )
        if isinstance(error, TimeoutError) and not step_timed_out:
            return None
        return self.known_issues.expected_failure(
            self.document["name"],
            result["index"],
            result["action"],
            result["error"],
            step_timed_out=step_timed_out,
        )

    def known_issue_summary(self):
        if self.known_issues is None:
            return None
        expected = [
            {k: v for k, v in e.items() if k != "compiled"}
            for e in self.known_issues.failures
            if e["scenario"] == self.document["name"]
        ]
        return {
            "baseline": self.known_issues.source,
            "firmware_sha256": self.known_issues.firmware_sha256,
            "applied": bool(self.known_failure or self.known_serial),
            "expected_failure": self.known_failure,
            "expected_failures_not_observed": [
                e
                for e in expected
                if not self.known_failure or e["step"] != self.known_failure["step"]
            ],
            "tolerated_serial": [
                {"regex": regex, **record} for regex, record in self.known_serial.items()
            ],
        }

    @staticmethod
    def guest_ns(out):
        try:
            return json.loads((out / "activity.json").read_text()).get("guest_ns")
        except (OSError, ValueError):
            return None

    def activity(self, context):
        context.checkpoint()
        try:
            value = json.loads((context.out / "activity.json").read_text())
        except FileNotFoundError:
            return None
        validate_identity(value)
        return value

    def action(self, context, kind, value, result):
        if kind in {"wait_serial", "expect_serial"}:
            while True:
                serial = context.serial()
                offset = self.serial_cursor if kind == "wait_serial" else 0
                match = serial_match(value, serial[offset:])
                result["actual"] = serial[-4000:]
                if match:
                    if match[0] == match[1]:
                        raise ExecutionFailed("serial regex must consume characters")
                    if kind == "wait_serial":
                        self.serial_cursor = offset + match[1]
                    return
                if kind == "expect_serial":
                    raise ExecutionFailed(f"serial expectation not observed: {value}")
                context.pause()
        elif kind == "wait_display":
            self.wait_display(context, value, result)
        elif kind == "http":
            self.http(context, value, result)
        elif kind == "assert_no_errors":
            serial = context.serial()
            end = serial.rfind("\n") + 1
            serial_errors(
                serial[self.error_cursor : end], value, self.known_issues, self.known_serial
            )
            self.error_cursor = end
            self.last_error_policy = value
            activity = self.activity(context)
            if activity is None:
                raise ExecutionFailed("model diagnostics are not available")
            result["actual"] = activity["counters"]
            if activity["counters"]["stalls"] or activity["counters"]["anomalies"]:
                raise ExecutionFailed(f"model faults: {activity['counters']}")
        else:
            start = self.guest_ns(context.out)
            while start is None:
                context.pause()
                start = self.guest_ns(context.out)
            target = start + int(value * 1_000_000_000)
            while (current := self.guest_ns(context.out)) is None or current < target:
                if current is not None and current < start:
                    raise ExecutionFailed("guest clock reset during sleep_guest")
                context.pause()
            result["actual"] = {"guest_start_ns": start, "guest_end_ns": current}

    def historical_settlements(self, context):
        path = context.out / "timeline.jsonl"
        try:
            with path.open("rb") as timeline:
                timeline.seek(self.timeline_offset)
                while True:
                    line = timeline.readline()
                    if not line or not line.endswith(b"\n"):
                        return
                    value = json.loads(line)
                    self.timeline_offset = timeline.tell()
                    validate_identity(value)
                    if value["settled"] and value.get("event") == "move_finished":
                        yield validate_state(value, context.hardware)
        except FileNotFoundError:
            return

    def wait_display(self, context, value, result):
        if value.get("allow_unchanged") and not self.command_baseline:
            raise ExecutionFailed("allow_unchanged requires a preceding mutating HTTP command")
        while True:
            if value.get("history"):
                states = self.historical_settlements(context)
            else:
                state = context.settlement()
                states = [state] if state else []
            for state in states:
                result["actual"] = state["text"]
                if state["movement_id"] <= 0 or (
                    state["sequence"] <= self.display_cursor and not value.get("allow_unchanged")
                ):
                    continue
                baseline = self.command_baseline
                unchanged = False
                if baseline and state["movement_id"] <= baseline["movement_id"]:
                    unchanged = bool(
                        value.get("allow_unchanged")
                        and baseline.get("state")
                        and state["movement_id"] == baseline["state"]["movement_id"]
                        and state["settlement_id"] == baseline["state"]["settlement_id"]
                        and state["text"] == baseline["state"]["text"]
                    )
                    if not unchanged:
                        continue
                result["flap_errors"] = [m["flap_error"] for m in state["modules"]]
                matches = display_matches(value["text"], state["text"])
                aligned = display_aligned(state, value["text"])
                result["alignment_failed"] = result.get("alignment_failed", False) or (
                    matches and not aligned
                )
                if matches and aligned:
                    self.display_cursor = state["sequence"]
                    self.command_baseline = None
                    result.update(
                        actual=state["text"],
                        state=state,
                        unchanged=unchanged,
                        alignment_failed=False,
                    )
                    return
            context.pause()

    def http(self, context, value, result):
        method = value.get("method", "GET")
        if method == "POST":
            self.serial_cursor = len(context.serial())
            activity = self.activity(context)
            if activity is None:
                raise ExecutionFailed("cannot establish pre-command model freshness")
            self.command_baseline = {
                "movement_id": activity["movement_id"],
                "state": context.settlement(),
            }
        data = json.dumps(value["body"]).encode() if "body" in value else None
        while True:
            context.checkpoint()
            try:
                actual = http_request(context, method, value["path"], data)
                result["actual"] = actual
                if actual["status"] != value["status"]:
                    raise ExecutionFailed(
                        f"HTTP status: expected {value['status']}, got {actual['status']}"
                    )
                if "json" in value:
                    try:
                        actual["json"] = json.loads(actual["body"], parse_constant=reject_constant)
                    except (ValueError, UnicodeDecodeError) as error:
                        raise ExecutionFailed("HTTP response is not valid JSON") from error
                    json_subset(value["json"], actual["json"])
                return
            except TimeoutError:
                context.checkpoint()
                if not value.get("wait_ready"):
                    raise ExecutionFailed("HTTP exchange timed out") from None
                context.pause()
            except (OSError, http.client.HTTPException) as error:
                result["actual"] = {"connection_error": str(error)}
                if not value.get("wait_ready"):
                    raise ExecutionFailed(f"HTTP transport failed: {error}") from error
                context.pause()

    def verify_final(self, out, hardware):
        serial = (out / "serial.log").read_text(errors="replace")
        serial = serial[: serial.rfind("\n") + 1]
        serial_errors(
            serial[self.error_cursor :],
            {"fresh_nvs": (self.last_error_policy or {}).get("fresh_nvs", False)},
            self.known_issues,
            self.known_serial,
        )
        state = read_settlement(out, hardware)
        if state and (state["counters"]["stalls"] or state["counters"]["anomalies"]):
            raise ExecutionFailed(f"model faults after execution: {state['counters']}")

    def finalize(self, out, manifest):
        from flapsim.report import write_reports

        write_reports(
            out,
            manifest,
            self.results,
            known_issues=self.known_issue_summary(),
        )
