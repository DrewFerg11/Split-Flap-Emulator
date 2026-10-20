#!/usr/bin/env python3
"""Build the pinned unmodified Xtensa QEMU and its callback launcher (Linux)."""

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/lcgamboa/qemu.git"


def run(args, cwd=None, capture=False):
    print("+", shlex.join(map(str, args)), flush=True)
    return subprocess.run(
        args, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE if capture else None
    ).stdout


def shared_link_command(command: str, build: Path) -> list[str]:
    """Reuse Ninja's exact link flags, following upstream's library build recipe."""
    tokens = shlex.split(command)
    expanded = []
    for token in tokens:
        expanded.extend(
            shlex.split((build / token[1:]).read_text()) if token[0] == "@" else [token]
        )
    main_objects = [t for t in expanded if t.endswith(".p/system_main.c.o")]
    if len(main_objects) != 1:
        raise ValueError("pinned QEMU link command must contain exactly one system main object")
    output = expanded.index("-o") + 1
    if expanded[output] != "qemu-system-xtensa":
        raise ValueError("unexpected QEMU link output")
    expanded[output] = "libqemu-xtensa.so"
    return [t for t in expanded if t not in main_objects and t != "-pie"] + ["-shared"]


def optional_features(help_text: str) -> list[str]:
    """Feature rows start with two spaces and a name; option rows begin with --."""
    features = re.findall(r"^  ([a-z][\w-]*)(?=\s|$)", help_text, re.MULTILINE)
    if not features:
        raise ValueError("no optional features found in pinned configure --help")
    return features


def build(args):
    started = time.monotonic()
    if sys.platform != "linux":
        raise ValueError("the verified QEMU integration currently requires Linux")
    pin = (ROOT / "qemu/QEMU_COMMIT").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", pin):
        raise ValueError("QEMU_COMMIT must contain one full lowercase commit SHA")
    if args.jobs < 1:
        raise ValueError("--jobs must be positive")
    cc = shlex.split(os.environ.get("CC", "cc"))
    for tool in ["git", "ninja", "pkg-config", cc[0]]:
        if shutil.which(tool) is None:
            raise ValueError(f"missing build dependency: {tool}; see docs/development.md")
    run(
        [
            "pkg-config",
            "--exists",
            "glib-2.0",
            "gthread-2.0",
            "libgcrypt",
            "slirp",
            "pixman-1",
            "json-c",
        ]
    )
    prefix = args.build_root.resolve()
    source, work = prefix / "src", prefix / "work"
    stage = args.install_dir.resolve() if args.install_dir else prefix / "install"
    # --clean discards only generated outputs. Never reset a developer's source edits.
    if args.clean:
        for directory in [work, stage]:
            if directory.exists():
                shutil.rmtree(directory)
    build_kind = "incremental" if (work / "build.ninja").exists() else "cold"
    prefix.mkdir(parents=True, exist_ok=True)
    if not (source / ".git").exists():
        run(["git", "init", str(source)])
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=source, capture_output=True, text=True
    )
    if head.returncode != 0:
        # A network failure after git init is recoverable on the next invocation.
        run(["git", "-C", str(source), "fetch", "--depth", "1", REPOSITORY, pin])
        run(["git", "-C", str(source), "checkout", "--detach", pin])
    if run(["git", "rev-parse", "HEAD"], source, True).strip() != pin:
        raise ValueError("QEMU source revision differs from pin; use a fresh --build-root")
    if run(["git", "status", "--porcelain", "--untracked-files=no"], source, True).strip():
        raise ValueError("QEMU source has local modifications; refusing an unrecorded build")
    work.mkdir(exist_ok=True)
    help_text = run([str(source / "configure"), "--help"], work, True)
    features = optional_features(help_text)
    flags = [
        "--target-list=xtensa-softmmu",
        "--extra-cflags=-fPIC",
        *[f"--disable-{feature}" for feature in features],
        "--enable-tcg",
        "--enable-system",
        "--disable-werror",
        "--enable-gcrypt",
        "--enable-slirp",
        f"--python={sys.executable}",
    ]
    configuration = {
        "commit": pin,
        "flags": flags,
        "cc": run([*cc, "--version"], capture=True),
        "cc_command": cc,
        "cflags": os.environ.get("CFLAGS", ""),
        "ldflags": os.environ.get("LDFLAGS", ""),
        "pkg_config_path": os.environ.get("PKG_CONFIG_PATH", ""),
    }
    stamp = work / "flapsim-config.json"
    if not stamp.exists() or json.loads(stamp.read_text()) != configuration:
        run([str(source / "configure"), *flags], work)
        stamp.write_text(json.dumps(configuration, indent=2) + "\n")
    run(["ninja", "-d", "keeprsp", "-j", str(args.jobs), "qemu-system-xtensa"], work)
    commands = run(["ninja", "-t", "commands", "qemu-system-xtensa"], work, True).splitlines()
    run(shared_link_command(commands[-1], work), work)
    for directory in [stage / "bin", stage / "lib", stage / "share/qemu"]:
        directory.mkdir(parents=True, exist_ok=True)
    model_sources = sorted((ROOT / "model").glob("*.c"))
    model_flags = shlex.split(
        run(["pkg-config", "--cflags", "--libs", "json-c", "glib-2.0"], capture=True)
    )
    run(
        [
            *cc,
            "-std=gnu11",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-isystem",
            str(source / "include"),
            "-isystem",
            str(source),
            "-isystem",
            str(work),
            str(ROOT / "qemu/launcher.c"),
            *map(str, model_sources),
            *model_flags,
            "-ldl",
            "-o",
            str(stage / "bin/flapsim-qemu"),
        ]
    )
    shutil.copy2(work / "libqemu-xtensa.so", stage / "lib")
    for rom in source.glob("pc-bios/esp32-v3-rom*.bin"):
        shutil.copy2(rom, stage / "share/qemu")
    sources = [
        ROOT / "qemu" / name
        for name in [
            "build.py",
            "build.sh",
            "launcher.c",
            "callbacks.h",
            "QEMU_COMMIT",
        ]
    ]
    sources.extend(sorted((ROOT / "model").glob("*.c")))
    sources.extend(sorted((ROOT / "model").glob("*.h")))
    manifest = {
        **configuration,
        "integration_sha256": hashlib.sha256(b"".join(p.read_bytes() for p in sources)).hexdigest(),
        "build_wall_seconds": round(time.monotonic() - started, 3),
        "build_kind": build_kind,
        "jobs": args.jobs,
        "host": {"architecture": platform.machine(), "logical_cpus": os.cpu_count()},
    }
    (stage / "build-info.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Built {stage / 'bin/flapsim-qemu'}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-root", type=Path, default=ROOT / "build/qemu")
    parser.add_argument(
        "--install-dir", type=Path, help="stage the runtime separately from the build cache"
    )
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 4))
    parser.add_argument(
        "--clean", action="store_true", help="discard generated build/install outputs"
    )
    args = parser.parse_args()
    try:
        build(args)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"QEMU build failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
