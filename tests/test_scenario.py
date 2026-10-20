"""Black-box scenario semantics against recorded hardware evidence and controlled HTTP."""

import copy
import json
import signal
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from test_runner import assert_reaped, cli_command

from flapsim.executor import ExecutionFailed, RunContext, load_executor
from flapsim.hardware import load_hardware
from flapsim.report import write_reports
from flapsim.runner import run
from flapsim.scenario import (
    GOLDEN_SHA256,
    ScenarioExecutor,
    json_subset,
    parse_variables,
    substitute,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures/golden-8-evidence.json").read_text())


def write_state(out, state, *, moving=False):
    out.mkdir(parents=True, exist_ok=True)
    for name in ["state", "activity"]:
        value = {**state, "settled": False} if name == "activity" and moving else state
        temporary = out / f"{name}.json.tmp"
        temporary.write_text(json.dumps(value))
        temporary.replace(out / f"{name}.json")


class ControlledProcess:
    returncode = None

    def poll(self):
        return self.returncode


@pytest.fixture
def context(tmp_path):
    out = tmp_path / "model"
    write_state(out, FIXTURE["states"][1])
    (out / "serial.log").write_text(FIXTURE["serial"])
    (out / "timeline.jsonl").write_text("".join(json.dumps(s) + "\n" for s in FIXTURE["states"]))
    return RunContext(
        out, FIXTURE["hardware"], "http://127.0.0.1:1", ControlledProcess(), time.monotonic() + 2
    )


def executor(steps, *, variables=(), **options):
    return ScenarioExecutor(
        {"version": 1, "name": "controlled", "timeout": 2, "steps": steps},
        FIXTURE["hardware"],
        variables,
        **options,
    )


@pytest.fixture
def server():
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://invalid.example/")
                    self.end_headers()
                    return
                if self.path == "/trickle-headers":
                    for byte in b"HTTP/1.1 200 OK\r\nX-Header: " + b"x" * 100:
                        self.connection.sendall(bytes([byte]))
                        time.sleep(0.01)
                    return
                body = (
                    b"invalid json"
                    if self.path == "/bad-json"
                    else b'{"version":"v1.4.0-rc.1","ok":true}'
                )
                self.send_response(500 if self.path == "/error" else 200)
                self.send_header(
                    "Content-Length", "1000" if self.path == "/trickle-body" else str(len(body))
                )
                self.end_headers()
                if self.path == "/trickle-body":
                    for _ in range(1000):
                        self.wfile.write(b"x")
                        time.sleep(0.01)
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            captured.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = b'{"type":"success"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}", captured
    finally:
        http.shutdown()
        http.server_close()
        thread.join(2)


def test_typed_variables_and_strict_substitution():
    values = parse_variables(["CENTER=true", 'WORDS=["HELLO"]', "VERSION=v1.4.0", "N=3"])
    assert substitute(
        {
            "body": {"center": "${CENTER}", "words": "${WORDS}"},
            "n": "${N}",
            "label": "tag ${VERSION}",
        },
        values,
    ) == {
        "body": {"center": True, "words": ["HELLO"]},
        "n": 3,
        "label": "tag v1.4.0",
    }
    for arguments in [["A=1", "A=2"], ["invalid"], ["X=NaN"], ["1x=foo"]]:
        with pytest.raises(ValueError):
            parse_variables(arguments)
    for value in ["${MISSING}", "${broken", "value ${N}", {"${N}": "value"}]:
        with pytest.raises(ValueError):
            substitute(value, values)


def test_variables_preserve_types_in_actual_http_body(context, server):
    context.endpoint = server[0]
    engine = executor(
        [
            {
                "http": {
                    "path": "/text",
                    "method": "POST",
                    "status": 200,
                    "body": {"center": "${CENTER}", "words": "${WORDS}"},
                }
            }
        ],
        variables=["CENTER=true", 'WORDS=["HELLO"]'],
    )
    engine.execute(context)
    assert server[1] == [{"center": True, "words": ["HELLO"]}]


def test_non_json_yaml_values_and_empty_serial_matches_are_rejected():
    import datetime

    with pytest.raises(ValueError, match="JSON values"):
        executor(
            [
                {
                    "http": {
                        "path": "/",
                        "method": "POST",
                        "status": 200,
                        "body": {"date": datetime.date(2026, 10, 20)},
                    }
                }
            ]
        )
    with pytest.raises(ValueError, match="consume characters"):
        executor([{"wait_serial": {"regex": "a*"}}])


@pytest.mark.parametrize(
    "step",
    [
        {"unknown": "X"},
        {"wait_serial": "X", "expect_serial": "X"},
        {"wait_serial": {"regex": "["}},
        {"wait_serial": "X", "timeout": False},
        {"wait_display": {"text": "HELLO"}},
        {"wait_display": {"text": "........", "allow_unchanged": 1}},
        {"http": {"path": "https://remote/", "status": 200}},
        {"http": {"path": "/", "status": True}},
        {"http": {"path": "/", "status": 200, "method": "POST", "wait_ready": True}},
        {"assert_no_errors": {"ignore_all": True}},
        {"sleep_guest": 0},
    ],
)
def test_invalid_steps_fail_before_execution(step):
    with pytest.raises(ValueError):
        executor([step])


def test_version_exception_requires_exact_pin_and_explicit_opt_in():
    step = {"expect_serial": "Firmware: ${FIRMWARE_VERSION} ("}
    strict = executor([step], variables=["FIRMWARE_VERSION=v1.4.0"], firmware_sha256=GOLDEN_SHA256)
    assert strict.version_exception is None
    assert strict.document["steps"][0]["expect_serial"]["contains"] == "Firmware: v1.4.0 ("
    allowed = executor(
        [step],
        variables=["FIRMWARE_VERSION=v1.4.0"],
        firmware_sha256=GOLDEN_SHA256,
        golden_exception=True,
    )
    assert allowed.version_exception["effective"] == "v1.4.0-rc.1"
    for digest, version in [("f" * 64, "v1.4.0"), (GOLDEN_SHA256, "v9.0.0")]:
        with pytest.raises(ValueError, match="golden version exception"):
            executor(
                [step],
                variables=[f"FIRMWARE_VERSION={version}"],
                firmware_sha256=digest,
                golden_exception=True,
            )


def test_wait_serial_consumes_new_matches_while_expect_uses_history(context):
    (context.out / "serial.log").write_text("AP ready\nAP ready\n")
    engine = executor(
        [{"wait_serial": "AP ready"}, {"wait_serial": "AP ready"}, {"expect_serial": "AP ready"}]
    )
    engine.execute(context)
    assert engine.serial_cursor == len("AP ready\nAP ready")
    engine = executor([{"wait_serial": "AP ready", "timeout": 0.1}])
    engine.serial_cursor = len((context.out / "serial.log").read_text())
    with pytest.raises(TimeoutError):
        engine.execute(context)


def test_old_software_reset_cannot_pass_after_mutating_http(context, server):
    context.endpoint = server[0]
    (context.out / "serial.log").write_text("Reset reason: software reset\nAP IP Address\n")
    engine = executor(
        [
            {"http": {"path": "/settings", "method": "POST", "body": {}, "status": 200}},
            {"wait_serial": "Reset reason: software reset", "timeout": 0.1},
        ]
    )
    with pytest.raises(TimeoutError):
        engine.execute(context)
    assert engine.results[0]["status"] == "passed" and engine.results[1]["status"] == "failed"


def test_history_observes_recorded_blank_before_later_startup_overwrites_it(context):
    engine = executor(
        [
            {"wait_display": {"text": "        ", "history": True}},
            {"wait_display": {"text": "XXXXXXXX"}},
        ]
    )
    engine.execute(context)
    assert [r["actual"] for r in engine.results] == ["        ", "XXXXXXXX"]
    assert engine.results[0]["state"]["settled"]


@pytest.mark.parametrize("allow,passes", [(False, False), (True, True)])
def test_cached_display_after_http_needs_explicit_idempotent_opt_in(context, server, allow, passes):
    context.endpoint = server[0]
    engine = executor(
        [
            {"wait_display": {"text": "XXXXXXXX"}},
            {
                "http": {
                    "path": "/text",
                    "method": "POST",
                    "body": {"words": ["XXXXXXXX"]},
                    "status": 200,
                }
            },
            {"wait_display": {"text": "XXXXXXXX", "allow_unchanged": allow}, "timeout": 0.1},
        ]
    )
    if passes:
        engine.execute(context)
        assert engine.results[-1]["unchanged"] is True
    else:
        with pytest.raises(TimeoutError):
            engine.execute(context)


def test_partial_row_or_old_settlement_cannot_satisfy_wait(context):
    write_state(context.out, FIXTURE["states"][1], moving=True)
    engine = executor([{"wait_display": {"text": "........"}, "timeout": 0.1}])
    with pytest.raises(TimeoutError):
        engine.execute(context)


def test_new_settlement_and_spaces_wildcards_are_checked_in_module_order(context):
    state = copy.deepcopy(FIXTURE["states"][1])
    baseline = {"movement_id": state["movement_id"], "state": state}
    engine = executor([{"wait_display": {"text": ".XXXXXXX"}, "timeout": 0.5}])
    engine.command_baseline = baseline
    state.update(
        movement_id=state["movement_id"] + 1,
        settlement_id=state["settlement_id"] + 1,
        sequence=state["sequence"] + 10,
    )
    timer = threading.Timer(0.05, write_state, args=(context.out, state))
    timer.start()
    try:
        engine.execute(context)
    finally:
        timer.join(1)
    assert not engine.results[0]["unchanged"]
    assert engine.results[0]["state"]["movement_id"] > baseline["movement_id"]


@pytest.mark.parametrize("path", ["/version", "/error", "/bad-json", "/redirect"])
def test_http_typed_json_and_negative_responses(context, server, path):
    context.endpoint = server[0]
    # Each independent response assertion gets a fresh setup/exchange budget.
    context.deadline = time.monotonic() + 10
    engine = executor(
        [
            {
                "http": {
                    "path": path,
                    "status": 200,
                    "json": {"version": "v1.4.0-rc.1", "ok": True},
                }
            }
        ]
    )
    if path == "/version":
        engine.execute(context)
    else:
        with pytest.raises(ExecutionFailed):
            engine.execute(context)


def test_http_json_boolean_is_distinct_from_number():
    with pytest.raises(ExecutionFailed, match="type mismatch"):
        json_subset({"ok": 1}, {"ok": True})


@pytest.mark.parametrize("path", ["/trickle-body", "/trickle-headers"])
def test_http_total_deadline_stops_trickle_responses(context, server, path):
    context.endpoint = server[0]
    engine = executor([{"http": {"path": path, "status": 200}, "timeout": 0.15}])
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        engine.execute(context)
    assert time.monotonic() - started < 0.8


def test_guest_sleep_uses_model_time_and_detects_early_exit(context):
    engine = executor([{"sleep_guest": 0.1, "timeout": 0.15}])
    with pytest.raises(TimeoutError):
        engine.execute(context)
    state = copy.deepcopy(FIXTURE["states"][1])
    state["guest_ns"] += 200_000_000
    timer = threading.Timer(0.05, write_state, args=(context.out, state))
    timer.start()
    try:
        executor([{"sleep_guest": 0.1}]).execute(context)
    finally:
        timer.join(1)
    context.process.returncode = 17
    with pytest.raises(RuntimeError, match="exited early"):
        executor([{"wait_serial": "missing"}]).execute(context)


def test_known_serial_errors_are_narrow_and_cannot_leak_past_reconfiguration(context):
    first = {"assert_no_errors": {"fresh_nvs": True, "default_missing_bus1": True}}
    engine = executor([first, {"assert_no_errors": {}}])
    engine.execute(context)
    with (context.out / "serial.log").open("a") as log:
        log.write("Error writing data to module 32, error code: 2\n")
    with pytest.raises(ExecutionFailed, match="unexpected serial error"):
        engine.verify_final(context.out, context.hardware)
    for text in [
        "Guru Meditation Error: Core panic\n",
        "[12][E][Preferences.cpp:51] begin(): nvs_open failed: NOT_FOUND\n",
    ]:
        (context.out / "serial.log").write_text(text)
        with pytest.raises(ExecutionFailed):
            executor([first]).execute(context)


def test_reports_escape_html_xml_and_keep_missing_partial_evidence_honest(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    manifest = {
        "passed": False,
        "exit_code": 3,
        "wall_seconds": 0.2,
        "error": "exit\x00",
        "scenario": {"name": "<test>"},
    }
    write_reports(tmp_path, manifest, [], None)
    root = ET.parse(tmp_path / "junit.xml").getroot()
    assert root.get("failures") == "1"
    assert "Timeline unavailable" in (tmp_path / "report.md").read_text()
    attack = copy.deepcopy(FIXTURE["states"][0])
    attack["modules"][0]["char"] = '</script><script>alert("injection")</script>'
    (tmp_path / "timeline.jsonl").write_text(json.dumps(attack) + "\n{")
    write_reports(tmp_path, manifest, [], {"reason": "explicit golden exception"})
    replay = (tmp_path / "replay.html").read_text()
    assert "</script><script>alert" not in replay and "\\u003c/script>" in replay
    assert "Incomplete trailing" in (tmp_path / "report.md").read_text()
    assert "EXPLICIT GOLDEN VERSION EXCEPTION" in summary.read_text()
    assert ET.parse(tmp_path / "junit.xml").getroot().get("failures") == "1"


@pytest.mark.parametrize("mode,code", [("success", 5), ("exit", 3), ("hang", 4)])
def test_run_finalizes_failure_and_not_run_steps_with_a_real_child(
    arguments, monkeypatch, mode, code
):
    monkeypatch.setenv("FLAPSIM_FAKE_MODE", mode)
    path = arguments.out.parent / "scenario.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "name": "negative",
                "timeout": 1,
                "steps": [
                    {"expect_serial": "deliberately absent"},
                    {"wait_serial": "never run"},
                ],
            }
        )
    )
    arguments.scenario = path
    arguments.timeout = 3
    assert run(arguments) == code
    value = json.loads((arguments.out / "run.json").read_text())
    assert value["steps"][-1]["status"] == "not_run"
    assert all(
        (arguments.out / name).is_file() for name in ["report.md", "junit.xml", "replay.html"]
    )
    junit = ET.parse(arguments.out / "junit.xml").getroot()
    assert int(junit.get("failures")) >= 1 and int(junit.get("skipped")) >= 1
    assert_reaped(value)


def test_wrong_display_has_nonzero_exit_and_expected_actual_evidence(arguments):
    path = arguments.out.parent / "display-negative.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "name": "wrong-display",
                "timeout": 3,
                "steps": [{"wait_display": {"text": "ZZZZZZZZ"}, "timeout": 1}],
            }
        )
    )
    arguments.scenario = path
    assert run(arguments) == 4
    value = json.loads((arguments.out / "run.json").read_text())
    assert value["steps"][0]["expected"]["text"] == "ZZZZZZZZ"
    assert value["steps"][0]["actual"] == "XXXXXXXX"
    assert ET.parse(arguments.out / "junit.xml").getroot().get("failures") == "1"
    assert_reaped(value)


def test_sigterm_during_http_stops_emulator_and_finalizes_reports(arguments):
    path = arguments.out.parent / "scenario.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "name": "signal",
                "timeout": 10,
                "steps": [
                    {"http": {"path": "/hang", "status": 200}},
                ],
            }
        )
    )
    command = cli_command(arguments)
    command[command.index("--scenario") + 1] = str(path)
    child = subprocess.Popen(command, stdout=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not (arguments.out / "flash-observed.json").exists():
            assert child.poll() is None and time.monotonic() < deadline
            time.sleep(0.02)
        child.send_signal(signal.SIGTERM)
        child.communicate(timeout=6)
        assert child.returncode == 143
        value = json.loads((arguments.out / "run.json").read_text())
        assert_reaped(value)
        assert ET.parse(arguments.out / "junit.xml").getroot().get("failures") == "1"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_checked_in_scenarios_load_with_the_required_version_variable():
    root = Path(__file__).resolve().parents[1]
    for name, board in [
        ("release-smoke-8", "esp32-8-single"),
        ("release-smoke-16", "esp32-16-dual"),
        ("homing-random", "esp32-8-single"),
    ]:
        load_executor(
            root / f"scenarios/{name}.yaml",
            load_hardware(board),
            variables=["FIRMWARE_VERSION=v1.4.0"],
            firmware_sha256=GOLDEN_SHA256,
            golden_exception=True,
        )
