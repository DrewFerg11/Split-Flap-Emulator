#!/usr/bin/env python3
"""Build Split-Flap Display firmware source as a Linux process against the native shims.

Every input is pinned or recorded: the firmware commit (verified for the golden tag), the
PlatformIO environment's defines, third-party library commits and a digest of the shims and
hardware model. The output directory holds bin/flapsim-native, littlefs/ and build-info.json.
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from native.sketch import platformio_env, preprocess_sketch  # noqa: E402

FIRMWARE_REPOSITORY = "https://github.com/DrewFerg11/Split-Flap-Display"
SKETCH = "src/SplitFlapDisplay.ino"
BUILD_SOURCE = "flapsim native"
# Only the original ESP32 is modelled; the C3/S3 environments are outside native mode too.
SUPPORTED_BOARDS = {"esp32dev"}
TAG_PATTERN = re.compile(r"v(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z]+)(?:\.(\d+))?)?")


def run(args, cwd=None, capture=False, env=None):
    print("+", shlex.join(map(str, args)), flush=True)
    return subprocess.run(
        list(map(str, args)),
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        env=env,
    ).stdout


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, text=True, capture_output=True
    ).stdout.strip()


def golden():
    return json.loads((ROOT / "tools/golden_firmware.json").read_text())


def fetch(url, ref, destination, commit=None):
    """Shallow-fetch one ref into a cache directory and verify the commit when it is pinned."""
    destination = Path(destination)
    if (destination / ".git").is_dir():
        try:
            head = git(destination, "rev-parse", "HEAD")
            clean = not git(destination, "status", "--porcelain")
            if clean and (commit is None and ref.startswith("refs/tags/") or head == commit):
                return head
        except subprocess.CalledProcessError:
            pass
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    run(["git", "init", "-q", destination])
    run(["git", "-C", destination, "remote", "add", "origin", url])
    refspec = f"+{ref}:{ref}" if ref.startswith("refs/") else ref
    run(["git", "-C", destination, "fetch", "-q", "--depth", "1", "origin", refspec])
    run(["git", "-C", destination, "checkout", "-q", "--detach", "FETCH_HEAD"])
    head = git(destination, "rev-parse", "HEAD")
    if commit is not None and head != commit:
        shutil.rmtree(destination)
        raise ValueError(f"{url} {ref} resolved to {head}, expected pinned {commit}")
    return head


def tag_rank(tag):
    major, minor, patch, label, number = TAG_PATTERN.fullmatch(tag).groups()
    return (int(major), int(minor), int(patch), label is None, label or "", int(number or 0))


def firmware_version(source):
    """The firmware's own build/scripts/inject_version.py rule: best release tag at HEAD."""
    try:
        described = git(source, "describe", "--tags", "--always", "--dirty")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"
    try:
        tags = [
            t
            for t in git(source, "tag", "--points-at", "HEAD").splitlines()
            if TAG_PATTERN.fullmatch(t)
        ]
    except subprocess.CalledProcessError:
        tags = []
    suffix = "-dirty" if described.endswith("-dirty") else ""
    if tags and described[: len(described) - len(suffix)] in tags:
        return max(tags, key=tag_rank) + suffix
    return described


def resolve_firmware(spec, cache):
    """`golden`, `git:<ref>` (of the firmware repository) or a local checkout path."""
    if spec == "golden":
        pin = golden()
        ref = f"refs/tags/{pin['tag']}"
        path = cache / f"firmware-{pin['source_commit']}"
        commit = fetch(f"https://github.com/{pin['repository']}", ref, path, pin["source_commit"])
        info = {
            "source": "golden",
            "repository": f"https://github.com/{pin['repository']}",
            "ref": pin["tag"],
        }
    elif spec.startswith("git:"):
        name = spec[4:]
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", name) or ".." in name:
            raise ValueError(f"invalid firmware git ref: {name!r}")
        if re.fullmatch(r"[0-9a-f]{40}", name):
            ref, pinned = name, name
        elif TAG_PATTERN.fullmatch(name):
            ref, pinned = f"refs/tags/{name}", None
        else:
            ref, pinned = f"refs/heads/{name}", None
        path = cache / ("firmware-ref-" + re.sub(r"[^A-Za-z0-9.-]", "_", name))
        if ref.startswith("refs/heads/") and path.exists():
            shutil.rmtree(path)  # branches move; always fetch their current tip
        commit = fetch(FIRMWARE_REPOSITORY, ref, path, pinned)
        info = {"source": "git", "repository": FIRMWARE_REPOSITORY, "ref": name}
    else:
        path = Path(spec).resolve()
        if not (path / SKETCH).is_file() or not (path / "platformio.ini").is_file():
            raise ValueError(f"{spec} is not a Split-Flap Display checkout")
        try:
            commit = git(path, "rev-parse", "HEAD")
        except (subprocess.CalledProcessError, FileNotFoundError):
            commit = None
        info = {"source": "path", "repository": None, "ref": None}
    dirty = None
    if commit:
        dirty = bool(git(path, "status", "--porcelain", "--untracked-files=no"))
    return path, {**info, "commit": commit, "dirty": dirty}


def libraries(cache):
    manifest = json.loads((ROOT / "native/libraries.json").read_text())
    resolved = []
    for library in manifest["libraries"]:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", library["name"]).strip("-").lower()
        path = cache / f"{slug}-{library['commit']}"
        fetch(library["repository"], library["commit"], path, library["commit"])
        resolved.append({**library, "path": path})
    return resolved


def shim_digest():
    """Identity of everything native mode adds around the firmware."""
    digest = hashlib.sha256()
    paths = sorted(
        p
        for p in [*(ROOT / "native").rglob("*"), *(ROOT / "model").glob("*.[ch]")]
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    )
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        digest.update(f"{relative}\0{len(path.read_bytes())}\0".encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def write_gzip(source, target):
    with source.open("rb") as raw, target.open("wb") as out:
        with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0) as compressed:
            shutil.copyfileobj(raw, compressed)


def littlefs(source, out, mode):
    """Mirror the firmware's gzip_littlefs.py layout: html/css/js gzipped only, json both."""
    target = out / "littlefs"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    if mode == "data":
        built = source / "src/web"
        files = sorted(built.glob("*.json"))
    else:
        npm = shutil.which("npm")
        if not npm:
            raise ValueError("--web-assets vite requires npm; use --web-assets data")
        work = Path(tempfile.mkdtemp(prefix="flapsim-web-"))
        try:
            for name in [
                "package.json",
                "package-lock.json",
                "vite.config.mjs",
                "tailwind.config.js",
            ]:
                if (source / name).exists():
                    shutil.copy2(source / name, work / name)
            shutil.copytree(source / "src/web", work / "src/web")
            run([npm, "ci", "--no-audit", "--no-fund"], cwd=work)
            run([npm, "run", "assets"], cwd=work)
            built = out / "web"
            if built.exists():
                shutil.rmtree(built)
            shutil.copytree(work / "build/web", built)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        files = sorted(p for p in built.iterdir() if p.is_file())
    for path in files:
        suffix = path.suffix.lstrip(".")
        if suffix in {"html", "css", "js", "json"}:
            write_gzip(path, target / (path.name + ".gz"))
        if suffix not in {"html", "css", "js"}:
            shutil.copy2(path, target / path.name)
    return [p.name for p in sorted(target.iterdir())]


def c_string(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def generate(source, out, firmware, environment, info):
    generated = out / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    sketch = source / SKETCH
    (generated / "SplitFlapDisplay.ino.cpp").write_text(
        preprocess_sketch(sketch.read_text(), sketch)
    )
    defines = {
        **environment["defines"],
        "FIRMWARE_VERSION": c_string(firmware["version"]),
        "FIRMWARE_BUILD_SOURCE": c_string(BUILD_SOURCE),
    }
    lines = ["/* Generated by native/build.py: PlatformIO build_flags for this environment. */"]
    lines += [f"#define {name} {value}" for name, value in defines.items()]
    (generated / "flapsim_firmware.h").write_text("\n".join(lines) + "\n")
    (generated / "flapsim_build_info.h").write_text(
        "#define FLAPSIM_NATIVE_BUILD_INFO " + c_string(json.dumps(info, sort_keys=True)) + "\n"
    )
    return generated


def compiler_identity():
    compiler = shlex.split(os.environ.get("CXX", "c++"))
    try:
        return run([*compiler, "--version"], capture=True).splitlines()[0]
    except (OSError, subprocess.CalledProcessError, IndexError):
        return "unknown"


def build(args):
    if sys.platform != "linux":
        raise ValueError("native mode currently requires Linux")
    for tool in ["git", "cmake", "pkg-config"]:
        if shutil.which(tool) is None:
            raise ValueError(f"missing build dependency: {tool}; see docs/native.md")
    cache = args.cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    source, firmware = resolve_firmware(args.firmware_src, cache)
    firmware["version"] = args.firmware_version or (
        golden()["tag"] if firmware["source"] == "golden" else firmware_version(source)
    )
    environment = platformio_env(source / "platformio.ini", args.env)
    if environment["board"] not in SUPPORTED_BOARDS:
        raise ValueError(
            f"environment {args.env} uses board {environment['board']}; only esp32dev is modelled"
        )
    deps = libraries(cache)
    out = (
        args.out or ROOT / "build/native" / f"{(firmware['commit'] or 'local')[:12]}-{args.env}"
    ).resolve()
    out.mkdir(parents=True, exist_ok=True)
    info = {
        "schema_version": 1,
        "firmware": firmware,
        "platformio": environment,
        "libraries": [{k: d[k] for k in ["name", "version", "commit", "license"]} for d in deps],
        "shim_sha256": shim_digest(),
        "web_assets": args.web_assets,
        "compiler": compiler_identity(),
        "sanitize": args.sanitize,
    }
    generated = generate(source, out, firmware, environment, info)
    info["littlefs"] = littlefs(source, out, args.web_assets)
    work = out / "cmake"
    generator = ["-G", "Ninja"] if shutil.which("ninja") else []
    run(
        [
            "cmake",
            "-S",
            ROOT / "native",
            "-B",
            work,
            *generator,
            "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
            f"-DFIRMWARE_SRC={source}",
            f"-DSKETCH_SOURCE={generated / 'SplitFlapDisplay.ino.cpp'}",
            f"-DGENERATED_DIR={generated}",
            "-DLIBRARY_INCLUDES=" + ";".join(str(d["path"] / d["include"]) for d in deps),
            "-DLIBRARY_SOURCES=" + ";".join(str(d["path"] / s) for d in deps for s in d["sources"]),
            f"-DFLAPSIM_SANITIZE={'ON' if args.sanitize else 'OFF'}",
        ]
    )
    run(["cmake", "--build", work, "--parallel", str(args.jobs)])
    (out / "bin").mkdir(exist_ok=True)
    shutil.copy2(work / "flapsim-native", out / "bin/flapsim-native")
    (out / "build-info.json").write_text(json.dumps(info, indent=2, sort_keys=True) + "\n")
    if args.test:
        run(
            [
                "cmake",
                "--build",
                work,
                "--parallel",
                str(args.jobs),
                "--target",
                "flapsim-shim-tests",
            ]
        )
        with tempfile.TemporaryDirectory(prefix="flapsim-shim-tests-") as scratch:
            run([work / "flapsim-shim-tests", scratch])
    print(
        f"native build: {out / 'bin/flapsim-native'} ({firmware['version']}, {firmware['commit']})"
    )
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--firmware-src",
        default="golden",
        help="golden (the pinned golden tag), git:<tag|branch|sha>, or a local checkout path",
    )
    parser.add_argument("--env", default="esp32_n4", help="PlatformIO environment for build_flags")
    parser.add_argument("--firmware-version", help="override the version the firmware reports")
    parser.add_argument("--web-assets", choices=["data", "vite"], default="data")
    parser.add_argument(
        "--out", type=Path, help="output directory (default build/native/<commit>-<env>)"
    )
    parser.add_argument("--cache", type=Path, default=ROOT / "build/native/cache")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--sanitize", action="store_true", help="address/undefined sanitizers")
    parser.add_argument("--test", action="store_true", help="also build and run the shim tests")
    args = parser.parse_args(argv)
    try:
        build(args)
    except (ValueError, subprocess.CalledProcessError, OSError) as error:
        print(f"native build failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
