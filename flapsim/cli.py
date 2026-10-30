"""Run real firmware with isolated flash, outputs and a bounded lifetime."""

import argparse
import os
import re
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
    runner.add_argument(
        "--scenario",
        required=True,
        help="boot, hold (run until interrupted) or a scenario YAML file",
    )
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
        "--i2c-history",
        action="store_true",
        help="retain I2C history and enable WebSocket telemetry (requires --control-port)",
    )
    runner.add_argument(
        "--qemu-prefix",
        type=Path,
        default=Path(os.environ.get("FLAPSIM_QEMU_PREFIX", ROOT / "build/qemu/install")),
    )
    panel = commands.add_parser(
        "panel", help="serve a live display for a run started with --control-port"
    )
    panel.add_argument("--run", type=Path, required=True, help="the running scenario's --out")
    panel.add_argument("--port", type=int, default=0, help="loopback port (0 chooses one)")
    panel.add_argument(
        "--controls",
        action="store_true",
        help="also forward reset, power, fault and mechanics commands from the opened page",
    )
    serve = commands.add_parser(
        "serve",
        help="keep an emulator running with the live panel and its controls (home-lab mode)",
    )
    serve.add_argument("--mode", choices=["qemu", "native"], default="qemu")
    serve.add_argument("--hardware", required=True)
    source = serve.add_mutually_exclusive_group()
    source.add_argument("--firmware", type=Path, help="factory image (qemu mode)")
    source.add_argument(
        "--firmware-release", metavar="TAG", help="download a Split-Flap Display release (qemu)"
    )
    source.add_argument("--native-build", type=Path, help="native/build.py output (native mode)")
    source.add_argument("--firmware-src", help="native mode: golden, git:<ref> or a checkout")
    serve.add_argument("--speed", type=float, help="native mode virtual clock rate")
    serve.add_argument("--timing", choices=["ci", "accurate", "realtime"], help="qemu mode")
    serve.add_argument("--seed", type=int)
    serve.add_argument("--out", type=Path, default=Path("runs/serve"))
    serve.add_argument("--port", type=int, default=8765, help="panel port (0 chooses one)")
    serve.add_argument(
        "--bind", default="127.0.0.1", help="panel listen address (default loopback only)"
    )
    serve.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="HOST:PORT",
        help="also answer requests addressed to this name (for --bind beyond loopback)",
    )
    serve.add_argument(
        "--qemu-prefix",
        type=Path,
        default=Path(os.environ.get("FLAPSIM_QEMU_PREFIX", ROOT / "build/qemu/install")),
    )
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 0

    if args.command == "serve":
        from flapsim.serve import ServeError, serve

        problem = serve_problem(args)
        if problem:
            print(f"Invalid serve: {problem}", file=sys.stderr)
            return 2
        try:
            return serve(args)
        except (ServeError, OSError) as error:
            print(f"Invalid serve: {error}", file=sys.stderr)
            return 2

    if args.command == "panel":
        from flapsim.panel import serve

        if not 0 <= args.port <= 65535:
            print("Invalid panel: --port must be in [0, 65535]", file=sys.stderr)
            return 2
        try:
            serve(args.run, args.port, controls=args.controls)
        except KeyboardInterrupt:
            return 0
        except OSError as error:
            print(f"Invalid panel: {error}", file=sys.stderr)
            return 2
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


def serve_problem(args):
    if not 0 <= args.port <= 65535:
        return "--port must be in [0, 65535]"
    if args.mode == "native" and (args.firmware or args.firmware_release):
        return "native mode builds firmware from source; use --native-build or --firmware-src"
    if args.mode == "native" and not (args.native_build or args.firmware_src):
        return "native mode needs --native-build or --firmware-src"
    if args.mode == "qemu" and (args.native_build or args.firmware_src):
        return "--native-build and --firmware-src are native mode options"
    if args.mode == "qemu" and not (args.firmware or args.firmware_release):
        return "qemu mode needs --firmware or --firmware-release"
    if any(not re.fullmatch(r"[A-Za-z0-9.-]{1,253}:\d{1,5}", h) for h in args.allow_host):
        return "--allow-host takes HOST:PORT, the address a browser uses for the panel"
    return None


def check_mode_arguments(args):
    """Each mode takes only its own inputs, so a run never silently ignores an option."""
    if getattr(args, "i2c_history", False) and args.control_port is None:
        raise ValueError("--i2c-history requires --control-port")
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
