"""Installed emulator identity, separate from input firmware and QEMU provenance."""

import json
import os
import re
from pathlib import Path

from flapsim import __version__


def emulator_provenance():
    result = {"emulator_version": __version__, "source_revision": "unrecorded"}
    if path := os.environ.get("FLAPSIM_PROVENANCE"):
        try:
            value = json.loads(Path(path).read_text())
        except (OSError, ValueError) as error:
            raise ValueError(f"cannot read emulator provenance: {error}") from error
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            or value.get("emulator_version") != __version__
            or not re.fullmatch(r"[0-9a-f]{40}|unrecorded", str(value.get("source_revision", "")))
        ):
            raise ValueError("invalid or mismatched emulator provenance")
        result = value
    return result
