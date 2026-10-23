# Stability and versioning

flapsim follows [Semantic Versioning](https://semver.org/). From 1.0.0, breaking changes to
anything marked **stable** below happen only in a new major version.

## Stable in 1.0

- **Modes.** QEMU mode (`--mode qemu`) and native mode (`--mode native`) both work.
- **CLI.** The `flapsim run` command, its options and its exit codes ([runner](runner.md)).
- **Scenario format.** The scenario file schema and built-in scenario names ([scenarios](scenarios.md)).
- **Hardware configuration.** The hardware YAML files ([hardware model](hardware-model.md)).
- **Known-issues baseline format** ([scenarios](scenarios.md#known-issue-baselines-golden-runs-only)).
- **Run outputs.** `run.json`, `state` and `timeline` files and `model-config.json`.
- **Container image.** A multi-arch image at `ghcr.io/drewferg11/split-flap-emulator` whose
  command is `flapsim`.

## Experimental in 1.0

The [live control API](control-api.md) and the [live display panel](panel.md) are shipped for early
use. They stabilize in a 1.x release once their open work is finished, so they may change between
1.x releases, including in incompatible ways.

## What a breaking change means

Every output schema carries an integer `schema_version` (currently `1` everywhere).

- A change that existing consumers can ignore (a new optional field, a new file) keeps the same
  `schema_version` and ships in a minor release.
- A change that can break a consumer (removing or renaming a field, changing a type or meaning)
  increments that schema's `schema_version` and ships only in a major release. Readers in flapsim
  reject a `schema_version` they do not understand rather than guess.
- Scenario files, hardware YAML and the baseline format are broken the same way: an incompatible
  change is a major release, and the release notes in [CHANGELOG.md](../CHANGELOG.md) say how to
  migrate.

## Not covered

Log text, the exact timing of emulated events, internal Python modules (`flapsim.*` beyond the CLI)
and anything under `docs/history/` are not part of the compatibility promise. Assert on settled
states, not timestamps (see [development](development.md#emulation-pitfalls)).
