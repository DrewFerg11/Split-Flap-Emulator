# Firmware release gate

Phase 6 is emulator issue [#7](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/7).
The emulator implementation is [PR #17](https://github.com/DrewFerg11/Split-Flap-Emulator/pull/17).
The companion workflow, [Split-Flap-Display #85](https://github.com/DrewFerg11/Split-Flap-Display/pull/85),
merged into the firmware's `main` as `d52dc78`. Its **Release / Tag** workflow now runs
the strict simulation on every `v*` tag push, before publication. It pins:

| Input | Pinned value |
|---|---|
| Gate helper | this repository at `3ba32a9` (`v0.1.0`), `tools/verify_release_gate.py` |
| Emulator image | `ghcr.io/drewferg11/split-flap-emulator@sha256:acd9a51fe50c91530df3e1583ce070cb17e712d31d8d415654c43d97879d3935` (v0.1.0) |

The gate is on the trusted release path, but #7's acceptance is not recorded yet: no
positive/negative hosted runs (dry runs or a gated release) are linked from #7. Golden
v1.4.1 itself failed the strict smokes because of SFD#54; v1.4.2 carries the fix and
passes them strictly in native mode (see
[retiring the baseline entries](scenarios.md#retiring-the-sfd54-and-sfd86-entries)).

## Inputs and trust

The firmware workflow retains its `release` environment approval, repository-owner
check and version-tag policy. All three existing chip builds remain. Only the
same-run `bins-esp32_n4` artifact is simulated, using the exact filename
`splitflap-esp32_n4-${GITHUB_REF_NAME}-factory.bin`. No rebuilt or downloaded golden
firmware can substitute for it. Each layout gets a separate artifact download,
container, private flash and fresh output directory on `ubuntu-24.04`.

The companion checks out this repository's standard-library helper
`tools/verify_release_gate.py` at a reviewed full commit SHA with persisted Git
credentials disabled. Its only registry input is `FLAPSIM_IMAGE`, set in the
workflow's simulation step, which must match `ghcr.io/drewferg11/split-flap-emulator@sha256:`
followed by 64 lowercase hex digits. Missing values and mutable tags fail closed. The
digest must come from phase 5's actual verified publication; a syntactically valid
value alone does not prove publication or verification.

The helper pulls that immutable Linux amd64 image, mounts the factory image
read-only and the output directory writable, and disables container networking.
It runs installed `release-smoke-8` and `release-smoke-16` resources in parallel
with `--timing ci`, a 300-second CLI deadline, and `FIRMWARE_VERSION` equal to the
authorized tag. `--known-issues` is not used.
The accepted golden regression baseline (#15) applies to emulator golden CI only;
it cannot authorize publication of a new firmware release.

## Publication and diagnostics

Both matrix children must succeed. A separate `publish-eligibility` job always
checks authorization, every chip build, and the aggregate simulation result;
failure, cancellation and skipping cannot produce its `publish=true` output.
The write-enabled release/Pages job also directly needs build and simulation,
and requires that explicit output. Simulation jobs have contents-read permission
only; a failure is never converted to success using `continue-on-error`.

`release-sim-8` and `release-sim-16` artifacts retain `evidence/` and runner CPU
specifications for 14 days, on success or failure. Evidence includes Docker output,
`release-gate.json`, the run manifest, serial/QEMU/model logs, state/activity,
timeline, JUnit, report and self-contained replay where available. Early failures
retain available evidence rather than inventing simulation output. The helper's
summary records digest, tag, layout, factory checksum, status and wall time. The
workflow also reports total simulation-job duration including checkout, image
pull and artifact upload. Upload occurs before that final duration summary, so
the duration is in the Actions summary rather than the uploaded artifact.

Positive runs and wrong-version probes require native QEMU exit0 after the
runner's shutdown. This is observed behavior of the pinned integration, verified
by the container verifier for expected CLI exits0/5/143; it is not a universal
QEMU SIGTERM contract. A changed QEMU pin must re-verify it before relaxing this
guard. An intentional timeout instead requires the boot-mode three-second run,
a failed manifest/JUnit, and integer native exit status agreeing with the last
reaped attempt. Boot mode has no timed-out step record; a forced reaped shutdown
may have a nonzero native status. Inconsistent/missing status, a different
scenario/budget, skipped evidence or Docker timeout cannot prove that probe.

Docker pull is bounded to 180 seconds and attached execution to 360 seconds;
other Docker operations are bounded to 30 seconds and named-container removal
runs on failure. The hosted job has a 15-minute final limit. Record real parallel
job durations and runner specifications before claiming the 15-minute acceptance
target; unit tests and old local image diagnostics are not timing evidence.

## Safe dry runs and negative proofs

On a trusted authorized version tag containing the companion workflow, manually
dispatch **Release / Tag** with `dry_run=true` (the manual default). The same approval,
builds, factory artifact and simulation path execute. With `sim_probe=none`, both
layouts must pass and publication eligibility must succeed while emitting
`publish=false`. Neither GitHub Release nor Pages runs. A normal tag push still
uses the production path; do not push an unreviewed test tag just to start a dry
run. This integration does not create tags or dispatch releases automatically.

Repeat manual dry runs on that trusted tag with each probe:

| Probe | Required rejection evidence |
|---|---|
| `wrong-version` | Actual simulator exit 5 at a serial version assertion with a deliberately mismatched expectation, and the full serial log reporting a parseable firmware version banner that differs from the probe's |
| `missing` | Missing exact factory path rejected before Docker; original same-run checksum retained |
| `corrupt` | Private deliberately corrupt copy rejected before Docker; original same-run artifact unchanged |
| `timeout` | Actual simulator exit 4 for its wall deadline, retained run reports and reaped native child |

Probe jobs intentionally remain failed. The independent `assert-rejection` job
requires evidence of the intended rejection in **both** layouts and a failed
`publish-eligibility` result. An unrelated image pull, Docker timeout, corrupt
manifest, missing report or cleanup failure cannot satisfy that assertion.
The overall negative workflow remains red by design; a successful assertion job
is the explicit negative proof. Probes are rejected outside manual dry runs.

Local reproduction after a real image is available:

```sh
# FLAPSIM_IMAGE must contain the actual verified immutable reference.
python3 tools/verify_release_gate.py run --image "$FLAPSIM_IMAGE" \
  --version v1.5.0 --artifact-dir /absolute/same-run-factory \
  --out /absolute/fresh-evidence --layout 8
```

Use layout `16` in a separate output directory. No registry login or credentials
are required for the intended public image. Failure to pull remains a blocker.

## Activation and updates

The companion has merged with the v0.1.0 image and helper pinned (above). What #7
still needs is evidence from trusted runs of that workflow: dry runs on an authorized
tag for both passing layouts (or the first gated release), all four asserted
rejection modes, preserved non-ESP32 builds and measured job durations, linked from
#7. Golden v1.4.1 could not provide the positive case because of SFD#54; a release
carrying the firmware fix, such as v1.4.2, can. Keep acceptance unchecked until those runs exist.

For an emulator update, publish/verify the new immutable image first, review its
source and helper diff, update `FLAPSIM_IMAGE` and both helper `ref:` pins together,
and repeat dry-run proofs before a production release. Record old/new references
and evidence on issue #7. Never put a placeholder digest in the workflow or
describe a pinned but unexercised gate as verified.
