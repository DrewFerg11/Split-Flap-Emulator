"""Expose concise retained run evidence in Actions logs, including failure context."""

import json
import sys
from pathlib import Path


def main():
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"Run manifest unavailable: {path}")
        return 0
    value = json.loads(path.read_text())
    print(
        json.dumps(
            {
                k: value.get(k)
                for k in [
                    "passed",
                    "error",
                    "exit_code",
                    "wall_seconds",
                    "source_unchanged",
                    "qemu_returncode",
                ]
            }
        )
    )
    for step in value.get("steps", []):
        summary = {
            k: step.get(k)
            for k in ["index", "action", "status", "error", "wall_seconds", "expected"]
        }
        summary["actual"] = str(step.get("actual"))[-1200:]
        print(json.dumps(summary))
    for name in ["state.json", "activity.json"]:
        try:
            state = json.loads((path.parent / name).read_text())
            print(
                name,
                json.dumps(
                    {
                        k: state.get(k)
                        for k in [
                            "text",
                            "rows",
                            "settled",
                            "movement_id",
                            "settlement_id",
                            "guest_ns",
                            "counters",
                        ]
                    }
                ),
            )
        except (OSError, ValueError):
            print(f"{name}: unavailable or malformed")
    if not value.get("passed"):
        try:
            print(
                "Last 50 serial lines:",
                "\n".join(
                    (path.parent / "serial.log").read_text(errors="replace").splitlines()[-50:]
                ),
                sep="\n",
            )
        except OSError:
            print("Serial output unavailable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
