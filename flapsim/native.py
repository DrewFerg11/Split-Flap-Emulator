"""Native mode: run firmware source built by native/build.py against the host shims."""

import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
from pathlib import Path

from flapsim.hardware import ROOT, write_model_config
from flapsim.process import group_exists, stop_group

SPEED_RANGE = (0.25, 50.0)
DEFAULT_SPEED = 5.0


def check_speed(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("--speed must be a number")
    if not SPEED_RANGE[0] <= value <= SPEED_RANGE[1]:
        raise ValueError(f"--speed must be in [{SPEED_RANGE[0]}, {SPEED_RANGE[1]}]")
    return float(value)


def source_root():
    """native/ and model/ sources: the checkout, or FLAPSIM_NATIVE_ROOT in the container."""
    return Path(os.environ.get("FLAPSIM_NATIVE_ROOT", ROOT))


def build_from_source(spec, out, log, *, jobs=None, timeout=1800):
    """Build `golden`, `git:<ref>` or a local checkout into `out` with native/build.py."""
    script = source_root() / "native/build.py"
    if not script.is_file():
        raise ValueError("--firmware-src needs the native sources; pass a prebuilt --native-build")
    command = [sys.executable, str(script), "--firmware-src", spec, "--out", str(out)]
    if jobs:
        command += ["--jobs", str(jobs)]
    with open(log, "w") as output:
        # Its own session, so a timeout or interrupt also stops git and the compiler tree.
        process = subprocess.Popen(
            command, stdout=output, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise ValueError(f"native build exceeded {timeout} s; see {log}") from error
        finally:
            if process.poll() is None or group_exists(process):
                stop_group(process)
    if returncode != 0:
        raise ValueError(f"native build failed ({returncode}); see {log}")
    return out


def load_build(path):
    """A native/build.py output directory: executable, LittleFS image and recorded provenance."""
    path = Path(path).resolve(strict=True)
    binary = path / "bin/flapsim-native"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError(f"missing native executable: {binary}")
    if not (path / "littlefs").is_dir():
        raise ValueError(f"missing native LittleFS directory: {path / 'littlefs'}")
    try:
        info = json.loads((path / "build-info.json").read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read native build provenance: {error}") from error
    firmware = info.get("firmware") if isinstance(info, dict) else None
    if (
        info.get("schema_version") != 1
        or not isinstance(firmware, dict)
        or not isinstance(firmware.get("version"), str)
        or not (firmware.get("commit") is None or re.fullmatch(r"[0-9a-f]{40}", firmware["commit"]))
        or not re.fullmatch(r"[0-9a-f]{64}", str(info.get("shim_sha256", "")))
    ):
        raise ValueError("invalid native build provenance")
    data = binary.read_bytes()
    return {
        "path": path,
        "binary": binary,
        "info": info,
        "binary_sha256": hashlib.sha256(data).hexdigest(),
        "binary_size": len(data),
    }


def listener():
    """The device's port 80: bound here and inherited, so reboots never lose the endpoint."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    server.set_inheritable(True)
    return server


def nvs_document(seed):
    """A scenario's settings seed in the shim's typed NVS file (str, i32, float32 blob)."""
    spaces = {}
    for space, values in seed.items():
        entries = spaces.setdefault(space, {})
        for key, value in values.items():
            if isinstance(value, str):
                entries[key] = {"type": "str", "value": value}
            elif isinstance(value, int):
                entries[key] = {"type": "i32", "value": value}
            else:
                entries[key] = {"type": "blob", "value": struct.pack("<f", value).hex()}
    return {"schema_version": 1, "namespaces": spaces}


def prepare(out, build, hardware, *, trace, speed, http_fd, seed, setup=None):
    """Write a fresh native run directory: model config, LittleFS copy and native config."""
    for name in [
        "state.json",
        "activity.json",
        "timeline.jsonl",
        "serial.log",
        "i2c.log",
        "nvs.json",
    ]:
        (out / name).unlink(missing_ok=True)
    littlefs = out / "littlefs"
    if littlefs.exists():
        shutil.rmtree(littlefs)
    shutil.copytree(build["path"] / "littlefs", littlefs)
    config = {
        "schema_version": 1,
        "model_config": str(write_model_config(hardware, out, trace=trace)),
        "serial_log": str(out / "serial.log"),
        "nvs": str(out / "nvs.json"),
        "littlefs": str(littlefs),
        "speed": speed,
        "random_seed": seed if seed is not None else 1,
        "http_fd": http_fd,
    }
    setup = setup or {}
    if setup.get("clock"):
        config["clock"] = setup["clock"]
    if setup.get("nvs"):
        (out / "nvs.json").write_text(json.dumps(nvs_document(setup["nvs"]), indent=2) + "\n")
    path = out / "native-config.json"
    path.write_text(json.dumps(config, indent=2) + "\n")
    return path


def ready(out):
    """The model has initialized (or resumed) and written its first activity record."""
    return (out / "activity.json").is_file() and (out / "serial.log").is_file()
