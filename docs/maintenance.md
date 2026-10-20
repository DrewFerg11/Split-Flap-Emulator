# Emulator maintenance

The maintainer merges the ordered [PR queue](implementation-queue.md). Keep
issues open until their acceptance evidence and merge requirements are met.
The strict golden smoke blocker is [firmware #54](https://github.com/DrewFerg11/Split-Flap-Display/issues/54):
GET `/settings` can return defaults after a confirmed persisted reconfiguration,
and dual-row POST `/text` can return success without physical movement. The
maintainer owns the firmware fix. Preserve the failing gates, captured evidence
and physical assertions; no runtime changes or baseline substitutions are part
of packaging.

## QEMU revision or integration updates

1. Read [ADR 0001](adr/0001-qemu-integration.md) and inspect the candidate fork
   revision before changing `qemu/QEMU_COMMIT`. The selected route uses an
   unmodified shared QEMU library with a separate C launcher/model. Confirm the
   callback ABI, exported initialization/main-loop/cleanup signatures, timer and
   reset hooks, Xtensa machine, ROM names and SLIRP forwarding still match.
   If a future route requires patches, include/review their complete source and
   application checks; do not silently change the chosen route.
2. Start a separate `mvp/` or maintenance branch. Record the old/new full pin,
   upstream source link, compatibility reason and affected schemas/fixtures.
   Run `qemu/build.sh --clean --jobs 4` for a clean rebuild, or remove only the
   disposable `build/qemu` tree before rebuilding if diagnosing a stale cache.
   Confirm supported command options with `qemu/build.sh --help`.
3. Run the Python suite, Ruff, clang-format 18 and actionlint as documented in
   [development](development.md). Inspect `build-info.json`, ROM staging,
   integration SHA and actual `--version`; a cached binary is not proof.
4. Fetch/check the pinned firmware with `python -m tools.verify_model --fetch-only`.
   Run both boot layouts, both strict golden smoke scenarios, and homing seeds
   7, 19 and 42 through CI, including strict version and controlled-failure
   checks. Review serial/state/activity/timeline provenance, fresh settlement
   IDs, zero unexpected stalls/anomalies, input integrity and child cleanup.
5. Rebuild and verify the installed container using [container instructions](container.md).
   Retain failing diagnostics and measured build/run times. Passing native tests
   alone does not establish image compatibility. Review fixture/schema changes
   explicitly and preserve readers' freshness/atomicity requirements.
6. After review and merge, release a new emulator version through verified
   publication. Record its digest before updating the companion firmware gate.

`qemu/build.sh` is a shell entrypoint into `qemu/build.py`; consult its current
help before invoking destructive clean options. Do not edit or erase `poc/`.

## Golden firmware updates and version exception retirement

`tools/golden_firmware.json` pins the public release, asset name, byte count and
SHA-256. The current asset is `splitflap-esp32_n4-v1.4.0-factory.bin`, 4,128,768
bytes, SHA-256
`a087801e2ba5e08154af28a636c310b988b43417f28c7ddb7bae4f3df9f6221e`.
Never replace a failed golden input opportunistically. A deliberate baseline
update is separate reviewed work: establish release provenance/checksum, explain
the behavior change, inspect the unmodified input, and run the full matrix and
negative cases. Golden version exceptions must match the exact checksum and
expected version. Retire the `v1.4.0`/`v1.4.0-rc.1` exception when the reviewed
baseline reports its release tag correctly. Firmware release gating never uses
this exception and tests the actual same-workflow factory artifact.

Do not overwrite a fixture with whatever a failing run produced. Explain model
or firmware behavior changes and test stale/incomplete state and controlled
negatives when updating expectations.

## Cache and build troubleshooting

Native CI keys retain OS/architecture, exact toolchain identity, QEMU pin,
build scripts/configuration and workflow. The exact key also hashes integration
sources; the bounded fallback drops only that integration-source suffix. The
integration is always rebuilt and its digest recorded. Incompatible core
configuration/toolchain caches must not be reused. Delete the affected Actions
cache or the disposable local build tree if a build is interrupted or corrupt;
do not bypass source checks to salvage it.

Docker uses BuildKit's `gha` cache, scope `flapsim-linux-amd64`. Base/dependency
layers precede native sources; Python/resources and revision labels follow the
native build so a CLI-only change does not rebuild QEMU. Pin/model/build-script
changes invalidate the native layer. Use `docker build --no-cache` for a clean
diagnostic. Disk-heavy source archives and the exact verified image archive can
require several GiB. There is no tested arm64 or multi-architecture image.

For early exits inspect `qemu.log`, `serial.log`, `run.json`, `build-info.json`
and Meson/configure diagnostics. Permission failures usually mean the external
firmware is unreadable or host output is not writable by the selected UID.
HTTP success without fresh physical movement is a firmware/test failure, not
permission to accept stale display state. A timeout must leave reports and
reaped children; unexpected cleanup failures are emulator defects to track.

## Workflows, evidence and protection

PR/main CI keeps fast checks, both real boot/smoke layouts and all seeded homing
jobs. The container job depends on those gates; known smoke failures prevent
image verification/publication and remain visible. The reusable container job
also tests installed resources, timeout, stop and wrong-version rejection.
Manual `Publish verified container` dispatch checks a trusted main commit without
publication. Version-tag runs add the publication step only after verification.

Suggested required checks for this phase are `Fast checks`,
`Golden scenario (smoke-8)`, `Golden scenario (smoke-16)`,
`Golden scenario (homing-7)`, `Golden scenario (homing-19)`,
`Golden scenario (homing-42)`, and the reusable
`Verified runtime image / Container smoke and lifecycle` job. Earlier phases
use `Pinned QEMU build and boot` layout jobs. Confirm exact displayed names
in a current Actions run before configuring branch protection; this work does
not change protection settings. A downstream skip is not a passed acceptance
criterion. Keep required checks aligned with the current workflow matrix.

Failure diagnostics are retained for 14 days, verified candidate image/proof
archives for 2 days, and publication results for 90 days. Download relevant
artifacts promptly for durable investigations. Reports include Markdown/JUnit,
serial, model timeline/state/activity, replay and run manifests. Reproduce the
same image/input/seed/timing; don't rebuild different bits and call them the
verified candidate. A rejected candidate must demonstrate nonzero verification
and zero publication attempts via `tools.verify_publication_gate`.

Measured evidence so far: initial clean foundation CI QEMU build 119.1 seconds
with four build jobs; runner CI real boots roughly 55/80 seconds for 8/16 modules.
The slow local two-CPU/vfs container prototype's combined native/wheel/archive
build step took 2137.8 seconds. Its Dockerfile predates the final split-cache
layout, so this is a diagnostic measurement, not a final image CI benchmark.
The exact-source local image at `39f9768` built in 4562.9 seconds on the same
two-CPU/vfs host and passed resource/provenance, lifecycle and rejection checks.
This is partial diagnostic evidence, not publication proof. Rebuild after source
changes and verify that image's identity against the recorded source revision.
Full successful smoke/image durations remain unavailable while #54 blocks them.
Record fresh CI timings and the immutable digest when acceptance passes; do not
claim the release gate's approximately 15-minute target from these partial runs.

[Emulator #14](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/14)
tracks a separate intermittent `Wire.cpp` I2C read Error 263 observed before
the 16-module smoke's HTTP command. Its cause is unknown; do not attribute it
to firmware #54, allowlist it, or retry until a run happens to pass. Preserve
the same source, firmware hash, seed, logs and model evidence when investigating.

## GPL source availability

The emulator/integration is GPL-2.0-only; see the repository `LICENSE`. QEMU is
obtained unmodified from [lcgamboa/qemu](https://github.com/lcgamboa/qemu) at
[`e39508f79cb0681bbe15cfcebd40a3f45cdc3c48`](https://github.com/lcgamboa/qemu/tree/e39508f79cb0681bbe15cfcebd40a3f45cdc3c48).
The runtime includes the license and the complete build-input source archives
at `/usr/share/doc/flapsim/sources/{qemu,flapsim}.tar.xz`, plus `provenance.json`.
The QEMU archive includes its submodule sources; the integration archive
includes launcher/model/build scripts, Python package, resources and Dockerfile.
Firmware licensing is separate; firmware is never distributed in this image.

To extract corresponding sources without starting a firmware run:

```sh
image_ref=ghcr.io/drewferg11/flapsim:<version>
container_id=$(docker create "$image_ref" --help)
docker cp "$container_id:/usr/share/doc/flapsim" ./flapsim-sources
docker rm "$container_id"
```

Use the recorded source SHA to obtain the matching repository checkout, inspect
the included QEMU pin/archives, then follow the clean build commands in
[container.md](container.md) and [development.md](development.md). Include these
notices and corresponding sources with any redistributed modified image.
