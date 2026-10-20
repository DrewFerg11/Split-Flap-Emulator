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
