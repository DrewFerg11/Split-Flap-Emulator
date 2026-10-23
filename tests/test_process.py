"""PID-reuse-safe process identity used by the lifecycle probes."""

import os
import subprocess
import sys
import time

from flapsim.process import parse_start_time, start_time, survives

# Fields after the command name, state first; start time is the 20th of them.
AFTER_COMM = " ".join(["S", *map(str, range(18)), "4242", "0"])


def test_start_time_parses_command_names_with_spaces_and_parentheses():
    for comm in ["qemu", "a b", "(x) y)", "p) 9 z"]:
        assert parse_start_time(f"123 ({comm}) {AFTER_COMM}\n".encode()) == 4242


def test_start_time_of_a_live_process_is_stable_and_missing_is_none():
    assert start_time(os.getpid()) == start_time(os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = child.pid
    child.wait()
    assert start_time(pid) is None


def test_running_original_child_survives():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert survives(child.pid, start_time(child.pid))
    finally:
        child.kill()
        child.wait()


def test_missing_child_does_not_survive():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    recorded = start_time(child.pid)
    child.wait()
    assert not survives(child.pid, recorded)


def test_reused_pid_of_another_process_does_not_count():
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        # The recorded identity was a process that has since exited; its PID is now this one.
        assert not survives(other.pid, start_time(other.pid) - 1)
    finally:
        other.kill()
        other.wait()


def test_unreaped_exited_child_still_counts_as_alive():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    recorded = start_time(child.pid)
    deadline = time.monotonic() + 5
    while (
        time.monotonic() < deadline
        and open(f"/proc/{child.pid}/stat").read().split(")")[1].split()[0] != "Z"
    ):
        time.sleep(0.01)
    try:
        assert survives(child.pid, recorded)
    finally:
        child.wait()
    assert not survives(child.pid, recorded)


def test_without_a_recorded_start_time_only_the_pid_is_compared():
    assert survives(os.getpid(), None)
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not survives(child.pid, None)
