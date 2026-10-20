"""Record the source revision supplied by a trusted checkout/build invocation."""

import argparse
import json
import re
import tomllib
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}|unrecorded", args.revision):
        parser.error("revision must be a full commit SHA or unrecorded for a local build")
    project = tomllib.loads(Path("pyproject.toml").read_text())
    value = {
        "schema_version": 1,
        "source_revision": args.revision,
        "emulator_version": project["project"]["version"],
        "repository": "https://github.com/DrewFerg11/Split-Flap-Emulator",
        "qemu_repository": "https://github.com/lcgamboa/qemu",
        "qemu_revision": Path("qemu/QEMU_COMMIT").read_text().strip(),
    }
    args.out.write_text(json.dumps(value, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
