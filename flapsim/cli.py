"""Command line entry point; the supervised runner is introduced in phase 3."""

import argparse

from flapsim import __version__


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run real Split-Flap Display firmware on virtual ESP32 hardware."
    )
    parser.add_argument("--version", action="version", version=f"flapsim {__version__}")
    parser.parse_args()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
