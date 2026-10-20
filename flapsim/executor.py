"""Executor boundary: supervision belongs to the runner, assertions to the executor."""

import json
import math
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from flapsim.hardware import HardwareLoader, mapping
from flapsim.state import read_settlement


class ExecutionFailed(RuntimeError):
    pass


class InterruptedRun(Exception):
    def __init__(self, signum):
        self.signum = signum
        super().__init__(f"interrupted by {signal.Signals(signum).name}")


def positive_timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout must be a finite positive number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be a finite positive number")
    return float(value)


@dataclass
class RunContext:
    out: Path
    hardware: dict
    endpoint: str
    process: object
    deadline: float
    interrupt: object = None

    def checkpoint(self):
        if self.interrupt and (signum := self.interrupt()) is not None:
            raise InterruptedRun(signum)
        if self.process.poll() is not None:
            raise RuntimeError(f"emulator exited early: {self.process.returncode}")
        if time.monotonic() >= self.deadline:
            raise TimeoutError("run exceeded its wall-clock deadline")

    def pause(self):
        self.checkpoint()
        time.sleep(min(0.05, max(0, self.deadline - time.monotonic())))

    def settlement(self):
        self.checkpoint()
        return read_settlement(self.out, self.hardware)

    def serial(self):
        self.checkpoint()
        try:
            return (self.out / "serial.log").read_text(errors="replace")
        except FileNotFoundError:
            return ""


class Executor(Protocol):
    timeout: float
    document: dict

    def execute(self, context: RunContext) -> dict: ...


@dataclass
class BootExecutor:
    timeout: float
    document: dict

    def execute(self, context):
        while True:
            state = context.settlement()
            if state and (state["counters"]["stalls"] or state["counters"]["anomalies"]):
                raise ExecutionFailed(f"model faults: {state['counters']}")
            if (
                state
                and state["movement_id"] > 0
                and "=== Split Flap Display ===" in context.serial()
                and (
                    self.document.get("expect_text") is None
                    or state["text"] == self.document["expect_text"]
                )
            ):
                return {"state": state}
            context.pause()


def load_executor(path, hardware):
    if str(path) == "boot":
        document = {"version": 1, "mode": "boot", "timeout": 240}
    else:
        try:
            document = yaml.load(Path(path).read_text(), Loader=HardwareLoader)
        except (OSError, yaml.YAMLError) as error:
            raise ValueError(f"cannot load scenario {path}: {error}") from error
    if not isinstance(document, dict):
        raise ValueError("scenario must be a mapping")
    mapping(
        {k: v for k, v in document.items() if k != "expect_text"},
        "boot scenario",
        {"version", "mode", "timeout"},
    )
    if type(document["version"]) is not int or document["version"] != 1:
        raise ValueError("scenario version must be 1")
    if document["mode"] != "boot":
        raise ValueError("only mode: boot is supported in this phase")
    expected = document.get("expect_text")
    if "expect_text" in document and (
        not isinstance(expected, str)
        or len(expected) != len(hardware["modules"])
        or any(c not in hardware["drum"]["flaps"] for c in expected)
    ):
        raise ValueError("expect_text must contain one physical flap character per module")
    # Also verifies that the resolved document is serializable before launch.
    json.dumps(document)
    return BootExecutor(positive_timeout(document["timeout"]), document)
