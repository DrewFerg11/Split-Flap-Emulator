"""Configuration and HTTP use one strict deterministic fault registry."""

import pytest

from flapsim.control import validate_command
from flapsim.faults import DEFAULT_FAULTS, resolved_faults, validate_faults
from flapsim.hardware import normalize_hardware


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"hall": None},
        {"motor": "slip"},
        {"present": 1},
        {"hall": "inverted"},
        {"read_corruption": True},
    ],
)
def test_fault_registry_rejects_unsupported_values_before_admission(value):
    with pytest.raises(ValueError):
        validate_faults(value, partial=True)
    with pytest.raises(ValueError):
        validate_command({"id": 1, "expected_revision": 0, "patch": {"faults": value}}, 0, 32)


def test_fault_config_is_literal_resolved_and_does_not_change_mechanical_draws():
    document = {
        "chip": "esp32",
        "buses": [{"id": 0, "modules": [32, 33]}],
        "drum": {
            "steps_per_rotation": 2048,
            "magnet_position": 730,
            "sensor_window": 40,
            "flaps": " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        },
        "start_angles": "random",
    }
    normal = normalize_hardware(document, seed=7)
    document["buses"][0]["modules"][0] = {"addr": 32, "faults": {"present": False}}
    faulted = normalize_hardware(document, seed=7)
    assert faulted["modules"][0].pop("faults") == {**DEFAULT_FAULTS, "present": False}
    assert faulted == normal
    assert resolved_faults({}) == DEFAULT_FAULTS


def test_reset_uart_fragment_requires_pinned_banner_known_prefix_and_observed_reset():
    from flapsim.scenario import serial_errors
    from tools.verify_faults import interrupted_reset_lines
    from tools.verify_native import KnownSerial

    line = "[ 37999][E][Preferenceets Jul 29 2019 12:21:46"
    text, fragments = interrupted_reset_lines(line, 1)
    assert text == "ets Jul 29 2019 12:21:46"
    assert fragments == [
        {"kind": "reset_interrupted_diagnostic", "fragment": "[ 37999][E][Preference"}
    ]
    serial_errors(text, {"fresh_nvs": True}, KnownSerial())
    with pytest.raises(RuntimeError):
        interrupted_reset_lines(line, 0)
    with pytest.raises(RuntimeError):
        interrupted_reset_lines(line + "\n" + line, 1)
    for bad in [
        "[1][E][Wire.cpp:3] failureets Jul 29 2019 12:21:46",
        "[1][E][Preferences.cpp:50] begin(): nvs_open failed: INVALID_HANDLE"
        "ets Jul 29 2019 12:21:46",
        "[1][E][Preferenceets invalid firmware banner",
    ]:
        preserved, fragments = interrupted_reset_lines(bad, 3)
        assert preserved == bad and not fragments
        with pytest.raises(RuntimeError):
            serial_errors(preserved, {}, KnownSerial())
