"""Configuration and HTTP use one strict deterministic fault registry."""

import pytest

from flapsim.control import validate_command
from flapsim.faults import (
    DEFAULT_FAULTS,
    resolved_faults,
    validate_effective_faults,
    validate_fault_counters,
    validate_faults,
)
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
        {"power": "drop"},
        {"power": "brownout"},
        {"power": "sag"},
        {"power": None},
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


def test_runtime_power_support_is_the_rail_switch_only():
    assert validate_faults({"power": "lost"}, partial=True) == {"power": "lost"}
    assert validate_faults({"power": "ok", "i2c": "ok"}, partial=True)
    command = {"id": 1, "expected_revision": 0, "patch": {"faults": {"power": "lost"}}}
    assert validate_command(command, 0, 32)["patch"] == {"faults": {"power": "lost"}}


def test_power_faults_keep_healthy_resolved_evidence_unchanged():
    healthy = {**DEFAULT_FAULTS, "seed": 9, "configured": True}
    assert validate_faults({**healthy, "i2c": "missing"})
    lost = validate_faults({**healthy, "power": "lost"})
    drop = {"power": "drop", "power_off_at_ns": 5, "power_on_at_ns": 9}
    assert validate_faults({**healthy, **drop})
    assert validate_faults({**healthy, "power": "brownout", "brownout": 0.5})
    for bad in [
        {**healthy, "power": "ok"},  # A healthy rail is omitted, not spelled out.
        {**healthy, "power": "drop", "power_off_at_ns": 9, "power_on_at_ns": 9},
        {**healthy, "power": "drop", "power_off_at_ns": 9},
        {**healthy, "power": "sag"},
        {**healthy, "power": "sag", "sag": 1.5},
        {**healthy, "power": "lost", "sag": 0.5},
        {**healthy, "brownout": 0.5},
    ]:
        with pytest.raises(ValueError):
            validate_faults(bad)
    initial = {**healthy, **drop, "power": "drop"}
    # Startup-only modes can be repaired at runtime but not swapped in.
    validate_effective_faults(initial, healthy)
    validate_effective_faults(healthy, lost)
    with pytest.raises(ValueError):
        validate_effective_faults(healthy, {**healthy, **drop})
    with pytest.raises(ValueError):
        validate_effective_faults(initial, {**initial, "power_on_at_ns": 10})


@pytest.mark.parametrize(
    "effects, faults, valid",
    [
        ({"blocked_steps": 0, "nacks": 0, "rng": 5, "events": 0}, {"seed": 5}, True),
        (
            {
                "blocked_steps": 0,
                "nacks": 1,
                "rng": 5,
                "events": 1,
                "power_losses": 1,
                "powered": True,
            },
            {"seed": 5},
            True,
        ),
        (
            {
                "blocked_steps": 0,
                "nacks": 0,
                "rng": 5,
                "events": 0,
                "power_losses": 0,
                "powered": False,
            },
            {"seed": 5},
            True,
        ),
        (
            {
                "blocked_steps": 0,
                "nacks": 0,
                "rng": 5,
                "events": 0,
                "power_losses": 0,
                "powered": True,
            },
            {"seed": 5, "power": "lost"},
            True,
        ),
        # The power fields appear exactly when they matter, and only together.
        (
            {"blocked_steps": 0, "nacks": 0, "rng": 5, "events": 0},
            {"seed": 5, "power": "lost"},
            False,
        ),
        (
            {
                "blocked_steps": 0,
                "nacks": 0,
                "rng": 5,
                "events": 0,
                "power_losses": 0,
                "powered": True,
            },
            {"seed": 5},
            False,
        ),
        (
            {"blocked_steps": 0, "nacks": 0, "rng": 5, "events": 0, "power_losses": 1},
            {"seed": 5},
            False,
        ),
        (
            {
                "blocked_steps": 0,
                "nacks": 0,
                "rng": 5,
                "events": 0,
                "power_losses": 1,
                "powered": 1,
            },
            {"seed": 5},
            False,
        ),
        (
            {
                "blocked_steps": 0,
                "nacks": 2,
                "rng": 5,
                "events": 1,
                "power_losses": 1,
                "powered": True,
            },
            {"seed": 5},
            False,
        ),
    ],
)
def test_power_evidence_is_present_exactly_when_it_matters(effects, faults, valid):
    if valid:
        validate_fault_counters(effects, faults)
    else:
        with pytest.raises(ValueError):
            validate_fault_counters(effects, faults)


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
    # The tear can land anywhere after the "[E][" tag, e.g. "Preferen" (55713) or just "[E][".
    for cut in ["[ 55713][E][Preferen", "[ 55713][E][P", "[ 55713][E]["]:
        kept, fragments = interrupted_reset_lines(f"{cut}\n{banner}", 1)
        assert cut not in kept and [f["fragment"] for f in fragments] == [cut]
    for text in [f"{fragment}\n{banner}\nrst:0xc", f"{fragment}\n\n{banner}", fragment]:
        kept, fragments = interrupted_reset_lines(text, 1)
        assert fragment not in kept
        assert [f["fragment"] for f in fragments] == [fragment]
    assert interrupted_reset_lines(f"{fragment}\n{banner}", 1)[1][0]["followed_by"] == banner
    # Followed by other output, it is not a reset tear and stays an error.
    other_output = f"{fragment}\nrst:0xc"
    assert interrupted_reset_lines(other_output, 1) == (other_output, [])
    # Beyond the observed resets, or not a prefix of the known diagnostic, it stays an error.
    with pytest.raises(RuntimeError):
        interrupted_reset_lines(f"{fragment}\n{banner}", 0)
    with pytest.raises(RuntimeError):
        interrupted_reset_lines(f"{fragment}\n{banner}\n{fragment}", 1)
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


def test_stalls_are_excused_only_as_far_as_the_golden_baseline_explains_them():
    from flapsim.baseline import load_known_issues
    from tools.verify_faults import unexcused_faults

    known = load_known_issues("baselines/v1.4.1-golden.yaml")
    serial = (
        "Error writing data to module 33, error code: 5\n"
        "Error writing data to module 34, error code: 2"
    )
    assert known.excused_stalls(serial)[0] == 1
    assert not unexcused_faults({"stalls": 0, "anomalies": 0}, known, serial)
    assert not unexcused_faults({"stalls": 1, "anomalies": 0}, known, serial)
    assert unexcused_faults({"stalls": 2, "anomalies": 0}, known, serial)
    assert unexcused_faults({"stalls": 0, "anomalies": 1}, known, serial)
    assert unexcused_faults({"stalls": 1, "anomalies": 0}, known, "")
    assert unexcused_faults({"stalls": 1, "anomalies": 0}, None, serial)
    # The firmware logs one failed write per module and boot; the model counts the rest (#79).
    lost = {"stalls": 2, "anomalies": 0, "lost_write_stalls": 2}
    assert not unexcused_faults(lost, known, serial)
    assert unexcused_faults(lost, known, "")
