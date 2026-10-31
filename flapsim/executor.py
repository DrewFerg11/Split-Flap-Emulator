"""Executor boundary: supervision belongs to the runner, assertions to the executor."""

import json
import math
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import yaml

from flapsim.hardware import HardwareLoader, mapping
from flapsim.resources import resolve_resource
from flapsim.state import display_aligned, read_settlement


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
    services: dict | None = None
    control: object = None

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


def excused(counters, known_issues, serial):
    """Whether a baseline explains every model fault: only drum stalls, each matched by a logged
    known I2C timeout (#14). Returns the tolerated-stall record, or None.

    The firmware logs only a module's first failed write per boot (SplitFlapModule::hasErrored), so
    one logged timeout can stand for several lost writes. Once the run has logged one, stalls
    the model saw follow an address-only write (lost_write_stalls, #79) are explained as well."""
    stalls = counters["stalls"]
    if counters["anomalies"] or not stalls or known_issues is None:
        return None
    allowance, issues = known_issues.excused_stalls(serial)
    lost = counters.get("lost_write_stalls", 0)
    if not allowance or stalls > max(allowance, lost):
        return None
    record = {"stalls": stalls, "allowance": allowance, "issues": issues}
    if lost:
        record["lost_write_stalls"] = lost
    return record


@dataclass
class BootExecutor:
    timeout: float
    document: dict
    known_issues: object = None
    tolerated_stalls: dict | None = None

    def execute(self, context):
        while True:
            state = context.settlement()
            if state and (state["counters"]["stalls"] or state["counters"]["anomalies"]):
                record = excused(state["counters"], self.known_issues, context.serial())
                if record is None:
                    raise ExecutionFailed(f"model faults: {state['counters']}")
                self.tolerated_stalls = record
            if (
                state
                and state["movement_id"] > 0
                and "=== Split Flap Display ===" in context.serial()
                and (
                    self.document.get("expect_text") is None
                    or (
                        state["text"] == self.document["expect_text"]
                        and display_aligned(state, self.document["expect_text"])
                    )
                )
            ):
                return {"state": state}
            context.pause()


# A hold runs until it is interrupted; its deadline only keeps the runner's arithmetic finite.
HOLD_TIMEOUT = 10 * 365 * 24 * 3600.0


@dataclass
class HoldExecutor:
    """Keep the firmware running, without checks, until the run is interrupted (`flapsim serve`
    and interactive use with the control API and panel)."""

    timeout: float = HOLD_TIMEOUT
    document: dict = field(default_factory=lambda: {"version": 1, "mode": "hold"})

    def execute(self, context):
        while True:
            context.pause()


def load_executor(
    path,
    hardware,
    *,
    variables=(),
    firmware_sha256=None,
    firmware_source=None,
    known_issues=None,
    mode="qemu",
    implicit_variables=None,
):
    if str(path) == "hold":
        if variables or known_issues is not None:
            raise ValueError("variables and known issues require a step-based scenario")
        return HoldExecutor()
    if str(path) == "boot":
        document = {"version": 1, "mode": "boot", "timeout": 240}
    else:
        try:
            document = yaml.load(
                resolve_resource("scenarios", path).read_text(), Loader=HardwareLoader
            )
        except (OSError, yaml.YAMLError) as error:
            raise ValueError(f"cannot load scenario {path}: {error}") from error
    if not isinstance(document, dict):
        raise ValueError("scenario must be a mapping")
    if "steps" in document:
        from flapsim.scenario import ScenarioExecutor

        return ScenarioExecutor(
            document,
            hardware,
            variables,
            firmware_sha256=firmware_sha256,
            firmware_source=firmware_source,
            known_issues=known_issues,
            mode=mode,
            implicit_variables=implicit_variables,
        )
    if variables or known_issues is not None:
        raise ValueError("variables and known issues require a step-based scenario")
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
