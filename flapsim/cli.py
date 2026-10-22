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
    runner.add_argument(
        "--mode",
        choices=["qemu", "native"],
        default="qemu",
        help="qemu: the released image on emulated ESP32 (release gate); native: firmware "
        "source built for the host (fast feedback)",
    )
    runner.add_argument("--firmware", type=Path, help="factory image (qemu mode)")
    runner.add_argument(
        "--native-build", type=Path, help="native/build.py output directory (native mode)"
    )
    runner.add_argument(
        "--firmware-src",
        help="native mode: build golden, git:<ref> or a checkout path first (source tree only)",
    )
    runner.add_argument(
        "--speed", type=float, help="native mode virtual clock rate, 0.25-50 (default 5)"
    )
    runner.add_argument("--hardware", required=True)
    runner.add_argument("--scenario", required=True, help="boot or a scenario YAML file")
    runner.add_argument("--var", dest="variables", action="append", default=[], metavar="KEY=value")
    runner.add_argument(
        "--known-issues",
        type=Path,
        help="checksum-bound baseline of linked known firmware issues (golden runs only)",
    )
    runner.add_argument("--out", type=Path, required=True)
    runner.add_argument("--timing", choices=["ci", "accurate", "realtime"], help="qemu mode")
    runner.add_argument("--seed", type=int)
    runner.add_argument("--timeout", type=float, help="override the scenario's total wall deadline")
    runner.add_argument("--trace", action="store_true", help="retain per-transaction I2C trace")
    runner.add_argument(
        "--control-port",
        type=int,
        help="enable the authenticated loopback state API (0 chooses a free port)",
    )
    runner.add_argument(
        "--qemu-prefix",
        type=Path,
        default=Path(os.environ.get("FLAPSIM_QEMU_PREFIX", ROOT / "build/qemu/install")),
    )
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 0

    if args.command == "run":
        try:
            check_mode_arguments(args)
        except ValueError as error:
            print(f"Invalid run: {error}", file=sys.stderr)
            return 2

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


def check_mode_arguments(args):
    """Each mode takes only its own inputs, so a run never silently ignores an option."""
    if args.mode == "qemu":
        if args.firmware is None:
            raise ValueError("qemu mode requires --firmware")
        extra = [
            n for n in ["native_build", "firmware_src", "speed"] if getattr(args, n) is not None
        ]
        if extra:
            raise ValueError(f"--{extra[0].replace('_', '-')} applies only to --mode native")
        args.timing = args.timing or "ci"
    else:
        if (args.native_build is None) == (args.firmware_src is None):
            raise ValueError("native mode requires exactly one of --native-build or --firmware-src")
        if args.firmware is not None or args.timing is not None:
            raise ValueError("--firmware and --timing apply only to --mode qemu")


if __name__ == "__main__":
    raise SystemExit(main())
