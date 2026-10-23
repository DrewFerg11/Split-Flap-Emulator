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
        {"sensor": None},
        {"motor": "slip"},
        {"i2c": 1},
        {"sensor": "inverted"},
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
    document["buses"][0]["modules"][0] = {"addr": 32, "faults": {"i2c": "missing"}}
    faulted = normalize_hardware(document, seed=7)
    configured = faulted["modules"][0].pop("faults")
    assert configured["i2c"] == "missing" and 0 < configured["seed"] <= 0xFFFFFFFF
    assert faulted == normal
    assert resolved_faults({}) == {**DEFAULT_FAULTS, "seed": 0, "configured": False}


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


def test_reset_uart_fragment_may_end_its_line_before_later_boot_output_or_the_capture():
    from tools.verify_faults import interrupted_reset_lines

    banner = "ets Jul 29 2019 12:21:46"
    fragment = "[ 39140][E][Preference"
    for text in [f"{fragment}\n{banner}\nrst:0xc", f"{fragment}\n\n{banner}", fragment]:
        kept, fragments = interrupted_reset_lines(text, 1)
        assert fragment not in kept
        assert [f["fragment"] for f in fragments] == [fragment]
    assert interrupted_reset_lines(f"{fragment}\nrst:0xc", 1)[1][0]["followed_by"] == "rst:0xc"
    # Beyond the observed resets, or not a prefix of the known diagnostic, it stays an error.
    with pytest.raises(RuntimeError):
        interrupted_reset_lines(f"{fragment}\n{banner}", 0)
    with pytest.raises(RuntimeError):
        interrupted_reset_lines(f"{fragment}\n{fragment}", 1)
    other = (
        "[ 39140][E][Wire.cpp:3] fail\n[ 39140][E][Preferences.cpp:50] begin(): nvs_open failed: X"
    )
    kept, fragments = interrupted_reset_lines(other, 3)
    assert kept == other and not fragments


def test_golden_baseline_tolerates_the_emulator_wire1_timeout_read_only():
    from flapsim.baseline import load_known_issues
    from flapsim.scenario import ExecutionFailed, serial_errors

    known = load_known_issues("baselines/v1.4.1-golden.yaml")
    read = "[ 52001][E][Wire.cpp:513] requestFrom(): i2cRead returned Error 263"
    serial_errors(read, {"fresh_nvs": True}, known)
    with pytest.raises(ExecutionFailed):
        serial_errors(
            "[ 52001][E][Wire.cpp:513] requestFrom(): i2cRead returned Error 1", {}, known
        )
