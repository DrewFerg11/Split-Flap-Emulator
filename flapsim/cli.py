"""Run real firmware with isolated flash, outputs and a bounded lifetime."""

import argparse
import os
import signal
import sys
from pathlib import Path

from flapsim import __version__
from flapsim.hardware import ROOT
from flapsim.runner import InterruptedRun, run


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run real Split-Flap Display firmware on virtual ESP32 hardware."
    )
    parser.add_argument("--version", action="version", version=f"flapsim {__version__}")
    commands = parser.add_subparsers(dest="command")
    runner = commands.add_parser("run", help="run a bounded firmware scenario")
    runner.add_argument("--firmware", type=Path, required=True)
    runner.add_argument("--hardware", required=True)
    runner.add_argument("--scenario", required=True, help="boot or a scenario YAML file")
    runner.add_argument("--var", dest="variables", action="append", default=[], metavar="KEY=value")
    runner.add_argument(
        "--golden-version-exception",
        action="store_true",
        help="explicitly accept v1.4.0-rc.1 only for the checksum-pinned v1.4.0 golden image",
    )
    runner.add_argument(
        "--known-issues",
        type=Path,
        help="checksum-bound baseline of linked known firmware issues (golden runs only)",
    )
    runner.add_argument("--out", type=Path, required=True)
    runner.add_argument("--timing", choices=["ci", "accurate", "realtime"], default="ci")
    runner.add_argument("--seed", type=int)
    runner.add_argument("--timeout", type=float, help="override the scenario's total wall deadline")
    runner.add_argument("--trace", action="store_true", help="retain per-transaction I2C trace")
    runner.add_argument(
        "--qemu-prefix",
        type=Path,
        default=Path(os.environ.get("FLAPSIM_QEMU_PREFIX", ROOT / "build/qemu/install")),
    )
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 0

    received = []

    def interrupted(signum, frame):
        # Record cancellation without raising between Popen and ownership assignment,
        # or interrupting cleanup. The bounded runner checks it at every wait.
        if not received:
            received.append(signum)

    previous = {s: signal.signal(s, interrupted) for s in [signal.SIGINT, signal.SIGTERM]}
    try:
        code = run(args, interrupt=lambda: received[0] if received else None)
    except (ValueError, OSError) as error:
        print(f"Invalid run: {error}", file=sys.stderr)
        return 2
    except InterruptedRun as error:
        return 128 + error.signum
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    print(f"{'PASS' if code == 0 else 'FAIL'}: evidence: {args.out / 'run.json'}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
