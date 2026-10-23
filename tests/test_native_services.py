"""Native scenario setup (seeded NVS, fixed clock, services) and the MQTT observer and steps."""

import json
import os
import shutil
import socket
import struct
import time
from pathlib import Path

import pytest

from flapsim import mqtt, native
from flapsim.executor import RunContext, load_executor
from flapsim.hardware import load_hardware
from flapsim.scenario import ExecutionFailed

HARDWARE = load_hardware("esp32-8-single")
BODY = """version: 1
name: s
timeout: 30
{head}steps:
{steps}"""


def scenario(tmp_path, head="", steps="  - sleep_guest: 1\n", **kwargs):
    path = tmp_path / "s.yaml"
    path.write_text(BODY.format(head=head, steps=steps))
    return load_executor(str(path), HARDWARE, **kwargs)


def test_native_setup_is_parsed_typed_and_native_only(tmp_path):
    executor = scenario(
        tmp_path,
        "modes: [native]\nservices: [mqtt]\nclock: {start: '2027-03-28T00:57:40Z'}\n"
        "nvs: {config: {ssid: lab, mqtt_port: '${MQTT_PORT}', maxVel: 12.5}}\n",
        mode="native",
        implicit_variables={"MQTT_PORT": 1883},
    )
    assert executor.native_setup == {
        "services": ["mqtt"],
        "nvs": {"config": {"ssid": "lab", "mqtt_port": 1883, "maxVel": 12.5}},
        "clock": {"mode": "fixed", "start": 1806195460, "sync": "boot"},
    }
    document = native.nvs_document(executor.native_setup["nvs"])["namespaces"]["config"]
    assert document["ssid"] == {"type": "str", "value": "lab"}
    assert document["mqtt_port"] == {"type": "i32", "value": 1883}
    assert document["maxVel"] == {"type": "blob", "value": struct.pack("<f", 12.5).hex()}


@pytest.mark.parametrize(
    "head,message",
    [
        ("nvs: {config: {ssid: x}}\n", "require modes: \\[native\\]"),
        ("modes: [qemu, native]\nclock: {start: '2027-01-01T00:00:00Z'}\n", "require modes"),
        ("modes: [native]\nservices: [mqtt, mqtt]\n", "services must be"),
        ("modes: [native]\nservices: [ftp]\n", "services must be"),
        ("modes: [native]\nnvs: {config: {waytoolongsettingname: 1}}\n", "1-15 character"),
        ("modes: [native]\nnvs: {config: {flag: true}}\n", "string, integer or float"),
        ("modes: [native]\nnvs: {config: {big: 4294967296}}\n", "32 bits"),
        ("modes: [native]\nnvs: [1]\n", "nvs must map"),
        ("modes: [native]\nclock: {start: 'tomorrow'}\n", "ISO 8601"),
        ("modes: [native]\nclock: {start: '2027-01-01T00:00:00+01:00'}\n", "in UTC"),
        (
            "modes: [native]\nclock: {start: '2027-01-01T00:00:00Z', sync: never}\n",
            "boot or station",
        ),
        ("modes: [native]\nclock: {start: '2027-01-01T00:00:00Z', at: 1}\n", "expected fields"),
    ],
)
def test_invalid_native_setup_fails_before_launch(tmp_path, head, message):
    with pytest.raises(ValueError, match=message):
        scenario(tmp_path, head, mode="native")


@pytest.mark.parametrize(
    "steps,message",
    [
        ("  - wait_mqtt: {topic: a}\n", "declare services: \\[mqtt\\]"),
        ("  - mqtt_publish: {topic: 'a/#', payload: x}\n", "wildcards"),
        ("  - mqtt_publish: {topic: a, payload: 3}\n", "payload must be a string"),
        ("  - wait_mqtt: {topic: a, payload: x, json: {}}\n", "either payload or json"),
        ("  - wait_mqtt: {topic: a, retained: yes-ish}\n", "retained must be boolean"),
    ],
)
def test_invalid_mqtt_steps_fail_before_launch(tmp_path, steps, message):
    head = "modes: [native]\nservices: [mqtt]\n" if "services" not in message else ""
    with pytest.raises(ValueError, match=message):
        scenario(tmp_path, head, steps, mode="native")


def test_runner_provided_variables_are_reserved(tmp_path):
    with pytest.raises(ValueError, match="MQTT_PORT is provided by the runner"):
        scenario(
            tmp_path,
            variables=["MQTT_PORT=1"],
            mode="native",
            implicit_variables={"MQTT_PORT": 2},
        )


class FakeObserver:
    def __init__(self, messages):
        self.messages = [{"index": i, "retained": False, **m} for i, m in enumerate(messages)]
        self.published = []
        self.error = None

    def snapshot(self):
        return list(self.messages)

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload, retain))


class Alive:
    returncode = None

    def poll(self):
        return None


def test_wait_mqtt_consumes_matches_in_order_and_publish_sends(tmp_path):
    executor = scenario(
        tmp_path,
        "modes: [native]\nservices: [mqtt]\n",
        "  - wait_mqtt: {topic: s, payload: two}\n"
        "  - wait_mqtt: {topic: c, json: {max: 8}}\n"
        "  - mqtt_publish: {topic: set, payload: HELLO, retain: true}\n",
        mode="native",
    )
    observer = FakeObserver(
        [
            {"topic": "s", "payload": "one"},
            {"topic": "c", "payload": '{"max": 16}'},
            {"topic": "s", "payload": "two"},
            {"topic": "c", "payload": "not json"},
            {"topic": "c", "payload": '{"max": 8, "x": 1}'},
        ]
    )
    context = RunContext(
        tmp_path, HARDWARE, "", Alive(), time.monotonic() + 5, None, {"mqtt": observer}
    )
    for step, result in zip(executor.document["steps"], executor.results, strict=True):
        executor.action(context, result["action"], step[result["action"]], result)
    assert executor.mqtt_cursor == 5
    assert observer.published == [("set", "HELLO", True)]
    # Consumed messages are never matched again: the next wait times out.
    late = RunContext(
        tmp_path, HARDWARE, "", Alive(), time.monotonic() + 0.2, None, {"mqtt": observer}
    )
    with pytest.raises(TimeoutError):
        executor.action(late, "wait_mqtt", {"topic": "s", "payload": "two"}, {})
    observer.error = "broken"
    with pytest.raises(ExecutionFailed, match="observer failed"):
        executor.action(context, "wait_mqtt", {"topic": "s"}, {})


def require_mosquitto():
    """Skip without a broker locally; CI sets FLAPSIM_REQUIRE_MOSQUITTO so a skip cannot hide."""
    if shutil.which("mosquitto") is None:
        if os.environ.get("FLAPSIM_REQUIRE_MOSQUITTO"):
            pytest.fail("mosquitto is required here (FLAPSIM_REQUIRE_MOSQUITTO is set)")
        pytest.skip("mosquitto is not installed")


def test_supervised_broker_and_observer_round_trip(tmp_path):
    require_mosquitto()
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        port = candidate.getsockname()[1]
    broker = mqtt.Broker(tmp_path, port)
    try:
        broker.wait_ready(RunContext(tmp_path, HARDWARE, "", broker.process, time.monotonic() + 10))
        observer = mqtt.Observer(port)
        observer.publish("splitflap/x/state", "HELLO", retain=True)
        deadline = time.monotonic() + 5
        while not observer.snapshot() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert observer.snapshot()[0] | {"monotonic": 0} == {
            "index": 0,
            "topic": "splitflap/x/state",
            "payload": "HELLO",
            "retained": False,
            "monotonic": 0,
        }
        late = mqtt.Observer(port, client_id="late")  # retained state reaches a new subscriber
        deadline = time.monotonic() + 5
        while not late.snapshot() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert late.snapshot()[0]["retained"] is True
        observer.close()
        late.close()
    finally:
        assert broker.stop() is not None
    assert "listener" in Path(tmp_path / "mosquitto.conf").read_text()
    assert json.dumps(broker.command)
