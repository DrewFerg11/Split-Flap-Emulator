# Container use and publication

Only Linux amd64 is supported and verified by the workflows. The runtime is
Debian bookworm-slim, runs as UID/GID 10001 by default, and has `flapsim` as its
entrypoint. Firmware stays external; no firmware binary is baked into the image.
Installed wheels include the hardware and scenario YAML, so commands work from
`/work` without mounting the source checkout.

**Publication is blocked.** The published golden firmware currently fails the
strict smoke gates described in [SFD #54](https://github.com/DrewFerg11/Split-Flap-Display/issues/54).
The maintainer will address that firmware defect. There is no verified published
image or digest yet. Do not treat the examples below as evidence of publication,
replace the baseline, retry commands to obtain a lucky pass, or relax assertions.

## Run a released image

After a verified version is published, substitute its recorded version for
`<version>` and the actual firmware release for `vX.Y.Z`. Prefer the immutable
`ghcr.io/drewferg11/flapsim@sha256:...` reference recorded by publication when
integrating a release gate.

```sh
firmware_path=$(realpath ./firmware.bin)
mkdir -p out/smoke-8 out/smoke-16
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$firmware_path,target=/firmware.bin,readonly" \
  --mount "type=bind,source=$(realpath out/smoke-8),target=/out" \
  ghcr.io/drewferg11/flapsim:<version> run \
  --firmware /firmware.bin --hardware esp32-8-single \
  --scenario release-smoke-8 --timing ci --var FIRMWARE_VERSION=vX.Y.Z \
  --out /out/run
```

For 16 modules, change the output mount to `out/smoke-16`, hardware to
`esp32-16-dual`, and scenario to `release-smoke-16`. AP-mode HTTP runs through
loopback inside the container; no published port or external networking is
needed. To use a custom YAML, mount it read-only and pass its container path.

The host creates output directories. `--user` makes files writable by the host
user; omitting it requires output permissions for 10001:10001. Firmware must be
readable by the selected user. Use absolute mount sources; paths containing
commas require Docker's CSV mount quoting. Each run requires a fresh output
directory. Guest flash writes go to a private copy under that directory.

Inspect `run/report.md`, `junit.xml`, `serial.log`, `timeline.jsonl`,
`replay.html`, and `run.json`. The manifest records emulator version/source,
QEMU revision/integration digest, firmware checksum, seed, timing and child
return codes. Open replay locally in a browser. Exit codes and deadlines follow
the [runner](runner.md) and [scenario](scenarios.md) contracts. `docker stop`
delivers TERM to the CLI, which stops/reaps QEMU and finalizes failure evidence;
the verified stop case expects CLI 143 and native QEMU 0.

Only the checksum-pinned published golden image may use this explicit version
exception:

```sh
# Add these arguments to a golden smoke command, not a firmware release gate:
--var FIRMWARE_VERSION=v1.4.0 --golden-version-exception
```

Its reported version is `v1.4.0-rc.1`. The exception is recorded and changes only
that known version mismatch; all functional smoke assertions remain strict.

## Build and verify locally

From a clean checkout on Linux amd64 with Docker BuildKit:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m tools.verify_model --fetch-only
source_revision=$(git rev-parse HEAD)
package_version=$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')
docker build --build-arg BUILD_JOBS=4 \
  --build-arg SOURCE_REVISION="$source_revision" \
  --build-arg EMULATOR_VERSION="$package_version" -t flapsim:candidate .
python -m tools.verify_container --image flapsim:candidate \
  --expected-source "$source_revision" --out runs/container
python -m tools.verify_publication_gate --image flapsim:candidate \
  --expected-source "$source_revision" --out runs/publication-gate
```

The first verifier resolves resources from installed site-packages, inspects
license/source archives, mounts only external firmware/output, and checks both
real smoke layouts, a timeout, TERM cleanup, and a deliberate version failure.
`--mode lifecycle` isolates timeout/stop diagnostics; `--mode reject` deliberately
fails candidate verification on the wrong version. Neither is publication
proof. `--timeout-scale` is an explicit local wall-budget diagnostic; publication
requires full mode with scale exactly 1. A slow host is not grounds to weaken
CI assertions. The second verifier proves an actual wrong-version candidate
fails before registry operations and checks workflow dependency/permissions.

Use `--docker-host` for a non-default local socket. A corporate CA can be passed
as a BuildKit `ca_bundle` secret; it is used for build-time Git/Python HTTPS and
is not copied into the image. Never disable TLS verification. The pinned base
image uses Debian's normal package repositories; a rebuild may receive newer
system security packages. Source/binary provenance identifies the actual image;
builds are not claimed to be bit-for-bit reproducible.

## Trusted publication

`publish.yml` accepts trusted `v*` tags whose commit belongs to `origin/main`
and whose SemVer version matches `pyproject.toml` (PEP 440 prerelease equivalence
is checked). A manual dispatch verifies only; it cannot push. No PR publishes.

The reusable verification workflow builds once, loads the candidate locally,
verifies that exact image ID, then exports it and its proof only on success.
The publication job downloads/loads the same archive; it does not rebuild.
Only that job has `packages: write`. It rejects incomplete, scaled, mismatched,
failed or incorrectly versioned proofs before registry writes. Existing version
tags are refused, including reruns; registry authorization/network failures fail
closed rather than being interpreted as absence. Version tags are immutable by
workflow policy; a digest is the immutable reference for consumers.

A stable `vX.Y.Z` updates `latest` only when no higher stable version is already
published. Prereleases, such as `v0.1.0-dev.0`, never update `latest`. Publication
runs serialize. The result artifact records image ID, source SHA, version,
registry digest and immutable reference. There is currently no result because
the strict firmware gates are blocked. After those gates pass and packaging is
merged, a trusted release and recorded digest are still required to complete
issue #6; the implementation PR alone does not close it.

See [maintenance](maintenance.md) for source licensing, cache recovery, evidence
retention, required checks and the firmware-release integration prerequisites.
