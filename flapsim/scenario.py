"""Validated, deadline-bound black-box scenarios for the real firmware.

The document schema lives in scenario_schema, the pure checks in scenario_checks and the
firmware HTTP exchanges in scenario_http; this module runs the steps.
"""

import copy
import http.client
import json
import time

from flapsim.executor import ExecutionFailed, RunContext, excused, positive_timeout
from flapsim.scenario_checks import display_matches, json_subset, serial_errors, serial_match
from flapsim.scenario_control import ACTIONS as CONTROL_ACTIONS
from flapsim.scenario_http import http_batch, http_request
from flapsim.scenario_schema import (
    MODES,
    STEPS,
    fields,
    native_setup,
    parse_variables,
    reject_constant,
    substitute,
    validate_step,
)
from flapsim.state import display_aligned, read_settlement, validate_identity, validate_state


class ScenarioExecutor:
    def __init__(
        self,
        document,
        hardware,
        variables,
        *,
        firmware_sha256=None,
        firmware_source=None,
        known_issues=None,
        mode="qemu",
        implicit_variables=None,
    ):
        if known_issues is not None:
            if mode == "native":
                source = firmware_source or {}
                known_issues.check_source(source.get("commit"), source.get("dirty"))
            else:
                known_issues.check_firmware(firmware_sha256)
        self.known_issues = known_issues
        self.known_serial = {}
        self.known_failure = None
        self.known_stalls = None
        requested = parse_variables(variables)
        implicit = implicit_variables or {}
        if reserved := sorted(implicit.keys() & requested.keys()):
            raise ValueError(f"--var {reserved[0]} is provided by the runner in this mode")
        requested = {**implicit, **requested}
        self.document = substitute(document, copy.deepcopy(requested))
        fields(
            self.document,
            {"version", "name", "timeout", "steps"},
            {"modes", "services", "nvs", "clock"},
        )
        modes = self.document.get("modes", list(MODES))
        if (
            not isinstance(modes, list)
            or not modes
            or len(set(modes)) != len(modes)
            or any(m not in MODES for m in modes)
        ):
            raise ValueError(f"scenario modes must be a nonempty list drawn from {list(MODES)}")
        if mode not in modes:
            raise ValueError(f"scenario {self.document['name']!r} does not support {mode} mode")
        if type(self.document["version"]) is not int or self.document["version"] != 1:
            raise ValueError("scenario version must be 1")
        if not isinstance(self.document["name"], str) or not self.document["name"]:
            raise ValueError("scenario name must be a nonempty string")
        self.timeout = positive_timeout(self.document["timeout"])
        if not isinstance(self.document["steps"], list) or not self.document["steps"]:
            raise ValueError("scenario steps must be a nonempty list")
        self.native_setup = native_setup(self.document)
        runtime_faulted = set()
        for step in self.document["steps"]:
            validate_step(
                step,
                hardware,
                self.native_setup["services"],
                mode=mode,
                runtime_faulted=runtime_faulted,
            )
            # Only preceding validated I2C injections authorize the existing narrow
            # write-error policy. Repair does not erase earlier serial evidence.
            if "set_fault" in step and step["set_fault"]["faults"].get("i2c") == "missing":
                runtime_faulted.add(step["set_fault"]["addr"])
            if (
                "set_mechanics" in step
                and step["set_mechanics"]["patch"].get("faults", {}).get("i2c") == "missing"
            ):
                runtime_faulted.add(step["set_mechanics"]["addr"])
        self.requires_control = any(set(step) & CONTROL_ACTIONS for step in self.document["steps"])
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
        self.mqtt_cursor = 0
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
                context.services,
                context.control,
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
            "firmware_source_commit": self.known_issues.firmware_source_commit,
            "applied": bool(self.known_failure or self.known_serial or self.known_stalls),
            "expected_failure": self.known_failure,
            "expected_failures_not_observed": [
                e
                for e in expected
                if not self.known_failure or e["step"] != self.known_failure["step"]
            ],
            "tolerated_serial": [
                {"regex": regex, **record} for regex, record in self.known_serial.items()
            ],
            "tolerated_stalls": self.known_stalls,
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
        if kind in CONTROL_ACTIONS:
            from flapsim.scenario_control import execute

            self.command_started(context)
            execute(context, kind, value, result)
        elif kind in {"wait_serial", "expect_serial"}:
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
        elif kind == "settings_stress":
            self.settings_stress(context, value, result)
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
            self.check_model(activity["counters"], serial[:end], "model faults")
        elif kind == "mqtt_publish":
            observer = self.observer(context)
            observer.publish(value["topic"], value["payload"], value.get("retain", False))
            result["actual"] = {"published": value}
        elif kind == "wait_mqtt":
            self.wait_mqtt(context, value, result)
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

    @staticmethod
    def observer(context):
        observer = (context.services or {}).get("mqtt")
        if observer is None:
            raise ExecutionFailed("the MQTT service is not running")
        return observer

    def wait_mqtt(self, context, value, result):
        """The first matching message after the previous match; earlier ones stay consumed."""
        observer = self.observer(context)
        while True:
            context.checkpoint()
            if observer.error:
                raise ExecutionFailed(f"MQTT observer failed: {observer.error}")
            messages = observer.snapshot()
            for message in messages[self.mqtt_cursor :]:
                if message["topic"] != value["topic"]:
                    continue
                if "retained" in value and message["retained"] != value["retained"]:
                    continue
                if "payload" in value and message["payload"] != value["payload"]:
                    continue
                if "json" in value:
                    try:
                        json_subset(
                            value["json"],
                            json.loads(message["payload"], parse_constant=reject_constant),
                        )
                    except (ValueError, ExecutionFailed):
                        continue
                self.mqtt_cursor = message["index"] + 1
                result["actual"] = {k: message[k] for k in ["topic", "payload", "retained"]}
                return
            result["actual"] = [
                {k: m[k] for k in ["topic", "payload", "retained"]}
                for m in messages
                if m["topic"] == value["topic"]
            ][-5:]
            context.pause()

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

    def command_started(self, context):
        self.serial_cursor = len(context.serial())
        activity = self.activity(context)
        if activity is None:
            raise ExecutionFailed("cannot establish pre-command model freshness")
        self.command_baseline = {
            "movement_id": activity["movement_id"],
            "state": context.settlement(),
        }

    def http(self, context, value, result):
        method = value.get("method", "GET")
        if method == "POST":
            self.command_started(context)
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

    def settings_stress(self, context, value, result):
        """Race settings writes against concurrent reads (SFD#54): each round POSTs a new `name`
        while `readers` GETs run, then reads it back. A read must never see anything but the
        previous or the new name (compiled defaults mean a raced read), and the final read
        must see the new one (anything else is a lost write)."""

        def settings(response, what):
            if isinstance(response, Exception):
                raise ExecutionFailed(f"{what}: HTTP transport failed: {response}")
            if response["status"] != 200:
                raise ExecutionFailed(f"{what}: HTTP status {response['status']}")
            try:
                return json.loads(response["body"], parse_constant=reject_constant)
            except (ValueError, UnicodeDecodeError) as error:
                raise ExecutionFailed(f"{what}: response is not valid JSON") from error

        summary = {"rounds": 0, "reads": 0}
        result["actual"] = summary
        previous = settings(
            http_request(context, "GET", "/settings", None), "initial GET /settings"
        ).get("name")
        for round_number in range(1, value["rounds"] + 1):
            name = f"stress-{round_number}"
            body = json.dumps({"name": name}).encode()
            requests = [("POST", "/settings", body)] + [("GET", "/settings", None)] * value.get(
                "readers", 4
            )
            write, *reads = http_batch(context, requests)
            reply = settings(write, f"round {round_number}: POST /settings")
            if reply.get("type") != "success":
                raise ExecutionFailed(f"round {round_number}: POST /settings replied {reply}")
            for read in reads:
                seen = settings(read, f"round {round_number}: GET /settings").get("name")
                if seen not in {previous, name}:
                    raise ExecutionFailed(
                        f"round {round_number}: GET /settings returned name {seen!r}, "
                        f"expected {previous!r} or {name!r} (raced read)"
                    )
                summary["reads"] += 1
            stored = settings(
                http_request(context, "GET", "/settings", None),
                f"round {round_number}: GET /settings after the write",
            ).get("name")
            if stored != name:
                raise ExecutionFailed(
                    f"round {round_number}: name is {stored!r} after POST {name!r} succeeded "
                    "(lost write)"
                )
            previous = name
            summary["rounds"] = round_number

    def verify_final(self, out, hardware):
        serial = (out / "serial.log").read_text(errors="replace")
        serial = serial[: serial.rfind("\n") + 1]
        serial_errors(
            serial[self.error_cursor :],
            {
                key: value
                for key, value in (self.last_error_policy or {}).items()
                if key in {"fresh_nvs", "faulted_modules"}
            },
            self.known_issues,
            self.known_serial,
        )
        state = read_settlement(out, hardware)
        if state:
            self.check_model(state["counters"], serial, "model faults after execution")

    def check_model(self, counters, serial, message):
        """Model faults fail the run, except drum stalls the baseline explains: each tolerated
        serial line with explains_stalls (an I2C timeout that lost a coil write, #14) excuses
        at most that many stalls. Anomalies are never excused."""
        if counters["stalls"] or counters["anomalies"]:
            record = excused(counters, self.known_issues, serial)
            if record is None:
                raise ExecutionFailed(f"{message}: {counters}")
            self.known_stalls = record

    def finalize(self, out, manifest):
        from flapsim.report import write_reports

        write_reports(
            out,
            manifest,
            self.results,
            known_issues=self.known_issue_summary(),
        )


# Callers and tests import these from here.
__all__ = [
    "ExecutionFailed",
    "ScenarioExecutor",
    "fields",
    "json_subset",
    "parse_variables",
    "serial_errors",
    "substitute",
    "validate_step",
]
