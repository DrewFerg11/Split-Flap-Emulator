# Emulator maintenance

The maintainer merges PRs. Keep
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
help before invoking destructive clean options.

## Golden firmware updates and version exception retirement

`tools/golden_firmware.json` pins the public release, asset name, byte count and
SHA-256. The current asset is `splitflap-esp32_n4-v1.4.1-factory.bin`, 4,128,768
bytes, SHA-256
`5a71579b127ee87f0716cb1df938c9f98d3519a9283f72c003dfb857db32e6f4`.
Never replace a failed golden input opportunistically. A deliberate baseline
update is separate reviewed work: establish release provenance/checksum, explain
the behavior change, inspect the unmodified input, and run the full matrix and
negative cases. Rebind the known-issues baseline to the new checksum and review
each entry: keep it while its linked issue is open and the firmware code behind it
is unchanged (many are intermittent, so one clean run is not proof of a fix), and
drop it when the linked fix ships.
The v1.4.0 `v1.4.0-rc.1` version exception was retired with the move to v1.4.1,
which reports its release tag. Firmware release gating tests the actual
same-workflow factory artifact.

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

Docker uses BuildKit's `gha` cache, one scope per platform (`flapsim-linux-amd64`,
`flapsim-linux-arm64`). Base/dependency
layers precede native sources; Python/resources and revision labels follow the
native build so a CLI-only change does not rebuild QEMU. Pin/model/build-script
changes invalidate the native layer. Use `docker build --no-cache` for a clean
diagnostic. Disk-heavy source archives and the exact verified image archive can
require several GiB, once per platform. arm64 builds and runs on GitHub's native
`ubuntu-24.04-arm` runners, which are free for public repositories only.

For early exits inspect `qemu.log`, `serial.log`, `run.json`, `build-info.json`
and Meson/configure diagnostics. Permission failures usually mean the external
firmware is unreadable or host output is not writable by the selected UID.
HTTP success without fresh physical movement is a firmware/test failure, not
permission to silently accept stale display state. The checksum-bound golden
baseline may explicitly report a documented expected failure; strict release
runs use no baseline. A timeout must leave reports and
reaped children; unexpected cleanup failures are emulator defects to track.

## Workflows, evidence and protection

PR/main CI keeps fast checks, both real boot/smoke layouts, the seed-7 homing
job and independent asserted strict/wrong-version negatives. The container job
depends on those gates. Golden scenarios use the accepted checksum-bound baseline
from #15; matched issues remain visible, while unmatched failures prevent image
verification/publication. Container golden checks mount that same baseline
read-only; version-rejection probes and future release gating remain strict. The reusable container job
also tests installed resources, timeout, stop and wrong-version rejection.
Manual `Release / Tag` dispatch checks a trusted main commit without
publication. Version-tag runs add the publication step only after verification.

Suggested required checks for this phase are `Fast checks`,
`Golden scenario (smoke-8)`, `Golden scenario (smoke-16)`,
`Golden scenario (homing-7)`, `Golden scenario (negative-versions)`, and the reusable
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
The later source `022a2b1` rebuilt in 189.12 seconds with the native cache and
passed the same partial checks. Its unchanged 120-second initial homing waits
timed out locally on both layouts (152.583/173.963 seconds total); model activity
was still moving, with only 21.8/15.8 seconds of guest time elapsed. These runs
did not reach HTTP assertions and do not establish a firmware regression. CI's
three homing seeds pass, while its full smokes fail on the firmware paths.
Those diagnostics predate the accepted #15 baseline. Record fresh image durations
and every applied known issue when the exact current candidate passes verification.
Record fresh CI timings and the immutable digest when acceptance passes; do not
claim the release gate's approximately 15-minute target from these partial runs.

[Emulator #14](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/14)
tracks the intermittent 16-module I2C timeouts (read Error 263, write code 5). Their
cause is established: QEMU's single-threaded core scheduling lets ESP-IDF's I2C ISR
on core 1 be parked mid-handler while the `Wire1` task on core 0 starts its next
transfer (see [scenarios.md](scenarios.md)). The golden baseline tolerates the lines
and the drum stalls that write timeouts cause, one per logged timeout. Re-check this
whenever the QEMU pin changes, and remove the entries once core scheduling is fixed.

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
image_ref=ghcr.io/drewferg11/split-flap-emulator:<version>
container_id=$(docker create "$image_ref" --help)
docker cp "$container_id:/usr/share/doc/flapsim" ./flapsim-sources
docker rm "$container_id"
```

Use the recorded source SHA to obtain the matching repository checkout, inspect
the included QEMU pin/archives, then follow the clean build commands in
[container.md](container.md) and [development.md](development.md). Include these
notices and corresponding sources with any redistributed modified image.

## Workflow naming

Workflows follow the same `Category / Purpose` display-name convention as
[numbers-go-up](https://github.com/DrewFerg11/numbers-go-up/tree/main/.github/workflows).

| File | Actions display name | Purpose |
|---|---|---|
| `ci-test.yml` | `CI / Test` | Fast checks, golden scenarios and image-verification dependency |
| `ci-container.yml` | `CI / Container` | Reusable exact-image smoke/lifecycle/publication-rejection checks |
| `ci-nightly.yml` | `CI / Nightly` | Scheduled soak and extra-seed scenarios kept off PR checks |
| `release.yml` | `Release / Tag` | Trusted tag validation and verified GHCR publication |

The firmware companion also uses `Release / Tag`. Job IDs/check names and trust
controls are preserved. Local reusable-workflow references, cache identity paths
and publication workflow validation point to the renamed files. The container
verification host explicitly installs `packaging>=25,<27`, which its publication
verifier imports; a runtime-only flapsim install does not provide that host tool.

## Release approval

Releases are owner-only, the same way as in Split-Flap-Display and numbers-go-up:

1. The `release tags` ruleset lets only admins create, move or delete `v*` tags.
2. `Release / Tag` starts with `authorize`, which uses the `release` environment.
   Every other job waits for it, so nothing is built or published until the owner
   approves the run in the Actions tab.
3. `authorize` also fails fast unless the actor is the repository owner and the ref
   is a `vX.Y.Z` or `vX.Y.Z-prerelease` tag.

`tools.verify_publication_gate` (run in CI by the packaging tests) fails if any job
stops depending on `authorize`, or if `authorize` or `publish` drops the
environment.

One-time environment settings (Settings → Environments → `release`):

- **Required reviewers:** the repository owner; leave **Prevent self-review** off so
  the owner can approve their own tag.
- **Deployment branches and tags:** selected tags only, rule `v*.*.*`.
- **Allow administrators to bypass:** off.

To release: first merge a change that sets `version` in `pyproject.toml` to the new
version, renames `## [Unreleased]` in [`CHANGELOG.md`](../CHANGELOG.md) to the new version and date
(adding a fresh empty `## [Unreleased]`), and checks [stability](stability.md) still matches the release
(the `validate` job rejects a tag that does not match it, and the `publish` job
refuses a version that is already published, but only after both architectures have been
built and verified), then push the tag
(`git tag -a vX.Y.Z -m ... && git push origin vX.Y.Z`), open the run, choose
**Review deployments → Approve**, then take the digest from the publish job's summary or
its `container-publication-result` artifact.

### Docker Hub mirror

Off unless configured, as in numbers-go-up. To enable it:

1. Create the Docker Hub repository (e.g. `drewferg11/split-flap-emulator`) and a
   personal access token with **Read, Write, Delete** scope (Delete is needed only to
   update the description).
2. In the `release` environment, add the secrets `DOCKERHUB_USERNAME` and
   `DOCKERHUB_TOKEN`.
3. Add the repository variable `DOCKERHUB_IMAGE` = `drewferg11/split-flap-emulator`.

`authorize` fails before anything is built if the variable is set but either secret is
missing, or if the name is not `<namespace>/<name>` in lowercase. Unset the variable to
publish to GHCR only.

### Pre-flight checklist for a release

Run through this before pushing a tag; the first release to use the multi-arch and
mirror path ([#63](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/63)) is the
dry run for the real one.

Repository settings (cannot be checked from the repository itself):

- [ ] The `release tags` ruleset covers `v*` and restricts create, update and delete to admins.
- [ ] The `release` environment lists the owner as required reviewer, allows deployments
      from tags matching `v*.*.*` only, and has administrator bypass off.
- [ ] Mirror only: repository variable `DOCKERHUB_IMAGE` is `<namespace>/<name>` in
      lowercase, the Docker Hub repository exists, and the `release` environment has
      `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (token scope Read, Write, Delete for the
      description sync). Leave the variable unset to publish to GHCR only.
- [ ] The GHCR package `split-flap-emulator` is public and still linked to this repository.
- [ ] Actions can run on `ubuntu-24.04-arm` (free for public repositories only).

Repository state:

- [ ] `main` CI is green at the commit to be tagged, and that commit is on `origin/main`.
- [ ] `pyproject.toml` `version` equals the tag without the `v`. A prerelease tag such as
      `v0.2.0-rc.1` matches version `0.2.0rc1`. Use a prerelease for a dry run: it never
      moves `latest`, whereas a stable tag does when no higher stable version exists.
- [ ] The version is not already published in GHCR or on Docker Hub. Nothing before the
      `publish` job checks this, so a duplicate wastes a full verification run. Versions are
      immutable, so a failed or abandoned run cannot be repeated under the same tag once
      any registry write has happened. Choose the next version instead.
- [ ] Optional: run `Release / Tag` by hand and select a version tag (it still waits for approval). That runs
      `authorize`, `validate` and both architectures' verification but never publishes.

Expected result for tag `vX.Y.Z` (version `X.Y.Z`):

| Where | Tags | Notes |
|---|---|---|
| `ghcr.io/drewferg11/split-flap-emulator` | `X.Y.Z-amd64`, `X.Y.Z-arm64` | One verified image each |
| same | `X.Y.Z` | Index of both; its digest is the reference to pin |
| same | `latest` | Stable releases only, and only if no higher stable version exists |
| `docker.io/$DOCKERHUB_IMAGE` | `X.Y.Z` (and `latest` as above) | Same digest as GHCR, or publication fails |

After the run:

- [ ] `container-publication-result` (also the publish job's summary) has `passed: true`,
      the source SHA, `platform_digests` for both architectures, the index `digest`,
      `latest_promoted` as expected and, with the mirror on, a `mirror_reference` with that
      same digest.
- [ ] `docker buildx imagetools inspect ghcr.io/drewferg11/split-flap-emulator:X.Y.Z` lists
      `linux/amd64` and `linux/arm64`, and the digest equals the recorded one. Repeat for
      `docker.io/$DOCKERHUB_IMAGE:X.Y.Z` when mirrored.
- [ ] Both pulls work anonymously, and `docker run --rm <ref> --help` works on an amd64 and
      an arm64 host (pull with `--platform linux/arm64` under emulation if no ARM host).
- [ ] The Docker Hub description matches the README, or the `::warning::` in the sync step
      explains why not.
- [ ] The digest is recorded in [container.md](container.md).
