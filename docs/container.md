# Container use and publication

The image is `ghcr.io/drewferg11/split-flap-emulator`; the command inside it is
`flapsim`, so `docker run <image> run ...` is `flapsim run ...`.

Linux amd64 and arm64 (for example a Raspberry Pi or an ARM home-lab host) are
supported. Each platform is built and fully verified on its own native runner, and a
release publishes both under one tag and one digest; Docker picks the right one. The runtime is
Debian bookworm-slim, runs as UID/GID 10001 by default, and has `flapsim` as its
entrypoint. Firmware stays external; no firmware binary is baked into the image.
Installed wheels include the hardware and scenario YAML, so commands work from
`/work` without mounting the source checkout.

**Published:** `v0.1.0` is `ghcr.io/drewferg11/split-flap-emulator:0.1.0` (also `latest`),
immutable reference `ghcr.io/drewferg11/split-flap-emulator@sha256:acd9a51fe50c91530df3e1583ce070cb17e712d31d8d415654c43d97879d3935`.
It predates multi-platform publishing, so it is linux/amd64 only; arm64 starts with the next release.
Its [Release / Tag run](https://github.com/DrewFerg11/Split-Flap-Emulator/actions/runs/39711368887) verified that exact image (installed resources, both golden
layouts, timeout, stop, wrong-version rejection and native mode) before publishing it, and the
image pulls anonymously. Split-Flap-Display pins it two ways:

- Its non-gating PR check (`ci-native-emulator.yml`) uses the readable
  `ghcr.io/drewferg11/split-flap-emulator:0.1.0@sha256:acd9a51fe50c91530df3e1583ce070cb17e712d31d8d415654c43d97879d3935`.
  Docker pulls by the digest; the tag is only for readers.
- The release gate ([Split-Flap-Display#85](https://github.com/DrewFerg11/Split-Flap-Display/pull/85))
  sets `FLAPSIM_IMAGE` to the digest-only reference above, because `tools.verify_release_gate`
  rejects any other form before contacting a registry.

Golden image checks explicitly mount the checksum-bound
[known-issues baseline](../baselines/v1.4.1-golden.yaml), following the accepted
[#15 policy](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/15).
Matched diagnostics and expected failures remain visible as
`passed_with_known_issues`/`xfail`, including the
[I2C scheduling artifact #14](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/14).
A baseline is not a firmware fix; new-firmware release checks use no baseline.

## Run a released image

After a verified version is published, substitute its recorded version for
`<version>` and the actual firmware release for `vX.Y.Z`. Prefer the immutable
`ghcr.io/drewferg11/split-flap-emulator@sha256:...` reference recorded by publication when
integrating a release gate.

```sh
firmware_path=$(realpath ./firmware.bin)
mkdir -p out/smoke-8 out/smoke-16
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$firmware_path,target=/firmware.bin,readonly" \
  --mount "type=bind,source=$(realpath out/smoke-8),target=/out" \
  ghcr.io/drewferg11/split-flap-emulator:<version> run \
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

Only the checksum-pinned published golden image may use the known-issues baseline:

```sh
# Add these arguments to a golden smoke command, not a firmware release gate:
--var FIRMWARE_VERSION=v1.4.1 --known-issues /known-issues.yaml
```

Add a read-only mount from the checked-out `baselines/v1.4.1-golden.yaml` to
`/known-issues.yaml` for this golden example. The baseline records exactly which
diagnostics/expected failures were observed. Omit it when testing a new firmware
release.

## Native mode in the image

The image also runs [native mode](native.md). It ships the shim and model sources
(`/opt/flapsim/native/src`), the pinned MIT-licensed libraries with recorded commits
(`/opt/flapsim/native/cache`), a C/C++ toolchain and Mosquitto. **It contains no firmware or
firmware-derived binary.** Mount a firmware checkout and the run builds it inside its own output
directory, with no network access:

```sh
git clone --depth 1 --branch v1.4.1 https://github.com/DrewFerg11/Split-Flap-Display fw-src
mkdir -p out/native-8
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$(realpath fw-src),target=/firmware-src,readonly" \
  --mount "type=bind,source=$(realpath out/native-8),target=/out" \
  ghcr.io/drewferg11/split-flap-emulator:<version> run --mode native --firmware-src /firmware-src \
  --hardware esp32-8-single --scenario release-smoke-8 --var FIRMWARE_VERSION=v1.4.1 \
  --out /out/run
```

The checkout must belong to the `--user` (git refuses other owners) and include the tag if the
version should read as a release (`--depth 1 --branch <tag>` keeps it). `golden`/`git:` sources
need network access and are meant for source checkouts. CI's container job runs
`tools.verify_container --mode native` against the candidate: `release-smoke-8`, `mqtt-native-8` and `runtime-controls-16`
build the golden source inside the image under `--network none`, as a separate evidence set that
does not change the publication contract.

## Build and verify locally

From a clean checkout on Linux amd64 with Docker BuildKit:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m tools.verify_model --fetch-only
source_revision=$(git rev-parse HEAD)
package_version=$(python -m setuptools_scm)
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
real smoke layouts, a declarative runtime-control scenario, a timeout, TERM cleanup,
and a deliberate version failure. Every case enables structured telemetry and
requires bounded complete journals, a flushed terminal cursor and stopped
control metadata; the control scenario also proves applied receipts, observed
disconnection/repair and reset/power persistence. Candidate proofs missing these
control checks are rejected before publication.
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

`release.yml` accepts trusted `v*` tags whose commit belongs to `origin/main`
and whose SemVer version is the version setuptools-scm derives from that tag (PEP 440
prerelease equivalence is checked, and a tag the checkout does not resolve to is rejected). Every run first waits in the `authorize` job for the owner's approval
through the `release` environment ([setup](maintenance.md#release-approval)); nothing
is built or published before that. A manual dispatch must select a version tag and
verifies only; it cannot push. No PR publishes.

The reusable verification workflow builds once per platform, on `ubuntu-24.04`
(amd64) and `ubuntu-24.04-arm` (arm64), loads each candidate locally, verifies that
exact image ID, then exports it and its proof only on success. The publication job
downloads and loads those same archives; it does not rebuild. It pushes each as
`X.Y.Z-amd64` / `X.Y.Z-arm64`, then joins them with `docker buildx imagetools create`
into one multi-platform index tagged `X.Y.Z`. That index digest is the reference to pin;
it covers both platforms.
Only that job has `packages: write`. It rejects incomplete, scaled, mismatched,
failed or incorrectly versioned proofs before registry writes. Existing version
tags are refused, including reruns; registry authorization/network failures fail
closed rather than being interpreted as absence. Version tags are immutable by
workflow policy; a digest is the immutable reference for consumers.

A stable `vX.Y.Z` updates `latest` only when no higher stable version is already
published. Prereleases, such as `v0.1.0-dev.0`, never update `latest`. Publication
runs serialize. The result artifact records image ID, source SHA, version,
registry digest and immutable reference; the first is `v0.1.0`'s
([run](https://github.com/DrewFerg11/Split-Flap-Emulator/actions/runs/39711368887), digest `sha256:acd9a51fe50c91530df3e1583ce070cb17e712d31d8d415654c43d97879d3935`).
Multi-platform releases also record each platform's digest.

When the `DOCKERHUB_IMAGE` repository variable is set (for example
`drewferg11/split-flap-emulator`), the same index is also tagged on Docker Hub in the
same `imagetools create` command, and publication fails unless the Docker Hub tags
resolve to the GHCR digest. The README is synced to the Docker Hub description (a
failure there only warns). Setup: [maintenance](maintenance.md#docker-hub-mirror).

See [maintenance](maintenance.md) for source licensing, cache recovery, evidence
retention, required checks and the firmware-release integration prerequisites.

## Verification history

The phase5 implementation merged as `dd6a432`. Its candidate passed installed
resources/source/license/provenance checks and all five image cases: smoke8
173.056s (all steps,24 visible known serial lines), smoke16 218.878s (visible
expected step8 failure), timeout exit4/3.023s, stop exit143/0.198s and wrong-version
exit5/6.163s. The overall job then failed at the host publication verifier's
missing `packaging` import. PR #17 fixes that dependency, verified in a clean
environment. These baseline results establish neither a firmware fix nor
publication authorization.

A subsequent candidate passed the corrected host setup, but the final serial
sweep caught the raw interleaving recorded in
[SFD #86](https://github.com/DrewFerg11/Split-Flap-Display/issues/86). Its narrow
checksum-bound baseline entry and regression/negative controls are documented in
[scenario usage](scenarios.md). Strict policy and release commands are unchanged.
The failed run skipped later lifecycle/rejection checks and published nothing.

[Issue #7](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/7) tracks exact
source/image identities, run links and temporary artifact pointers (14-day
retention), alongside the acceptance evidence still required. Fresh current-source
image/rejection verification and actual immutable publication are necessary;
old partial proof cannot authorize publication or establish an active release gate.
