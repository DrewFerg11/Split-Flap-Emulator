"""Linux process-group supervision, including reaping adopted descendants."""

import ctypes
import errno
import os
import signal
import subprocess
import sys
import time


def enable_subreaper():
    """Keep orphaned emulator descendants reapable by this supervisor on Linux."""
    if sys.platform != "linux":
        raise ValueError("the production QEMU runner currently requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot enable child subreaper")


def parse_start_time(stat):
    return int(stat.rsplit(b")", 1)[1].split()[19])


def start_time(pid):
    """Return the kernel start time (clock ticks since boot) of a process, or None if it is gone.

    Together with the PID this identifies one process even after the PID is reused. The command
    name in /proc/<pid>/stat may contain spaces and parentheses, so fields are counted from the
    last closing parenthesis; start time is field 22 overall, which is index 19 after it.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as stat:
            return parse_start_time(stat.read())
    except (FileNotFoundError, ProcessLookupError):
        return None


def survives(pid, recorded_start_time):
    """True while the process recorded as (pid, start time) still exists, even as a zombie.

    A reused PID has a different start time and so does not count. Without a recorded start
    time (older manifests) only the PID can be compared.
    """
    current = start_time(pid)
    if current is None:
        return False
    return recorded_start_time is None or current == recorded_start_time


def signal_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def group_exists(process):
    try:
        os.killpg(process.pid, 0)
        return True
    except ProcessLookupError:
        return False


def reap_descendants(process):
    # Popen owns the direct child. Never consume its wait status behind its back.
    if process.poll() is None:
        return
    while True:
        try:
            pid, _ = os.waitpid(-process.pid, os.WNOHANG)
            if pid == 0:
                return
        except ChildProcessError:
            return
        except OSError as error:
            if error.errno != errno.EINTR:
                raise


def stop_group(process, *, grace=2.0, kill_grace=2.0):
    """Stop the whole group even when its original leader has already exited."""
    signal_group(process, signal.SIGTERM)
    for signum, seconds in [(signal.SIGTERM, grace), (signal.SIGKILL, kill_grace)]:
        signal_group(process, signum)
        deadline = time.monotonic() + seconds
        while True:
            reap_descendants(process)
            if process.poll() is not None and not group_exists(process):
                return process.returncode
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
    try:
        process.wait(timeout=0.1)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("emulator did not exit after SIGKILL") from error
    reap_descendants(process)
    if group_exists(process):
        raise RuntimeError("emulator descendants survived cleanup")
    return process.returncode
