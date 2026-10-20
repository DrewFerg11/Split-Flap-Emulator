"""Golden-only serial allowances must not hide unrelated firmware errors."""

import pytest

from flapsim.hardware import load_hardware
from tools.verify_model import check_serial_errors


def test_exact_eight_module_missing_write_errors_allowed():
    errors = "\n".join(f"Error writing data to module {a}, error code: 2" for a in range(32, 40))
    check_serial_errors(errors, load_hardware("esp32-8-single"))
    with pytest.raises(RuntimeError, match="write errors"):
        check_serial_errors(
            errors + "\nError writing data to module 32, error code: 2",
            load_hardware("esp32-8-single"),
        )
    with pytest.raises(RuntimeError, match="write errors"):
        check_serial_errors(errors, load_hardware("esp32-16-dual"))


def test_only_known_empty_nvs_error_is_allowed():
    hardware = load_hardware("esp32-16-dual")
    known = "[  3326][E][Preferences.cpp:50] begin(): nvs_open failed: NOT_FOUND"
    check_serial_errors(known, hardware)
    for line in [
        known.replace("NOT_FOUND", "NO_FREE_PAGES"),
        known.replace(":50", ":51"),
        "Guru Meditation Error: Core 0 panic'ed",
        "Error writing data to module 32, error code: 4",
    ]:
        with pytest.raises(RuntimeError, match="unexpected firmware serial error"):
            check_serial_errors(line, hardware)
