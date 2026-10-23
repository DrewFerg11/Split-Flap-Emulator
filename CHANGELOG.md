# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/) as described in [docs/stability.md](docs/stability.md).

## [Unreleased]

Merged to `main` after the `v0.1.0` tag (`3ba32a9`) and not yet released. The version in
`pyproject.toml` is still `0.1.0`; it changes in the release PR.

### Added

- Control API (**experimental**): shared live state and mechanics control in native runs, audited
  controller reset, power off/on, native runtime speed and runtime fault controls (#37, #43, #51,
  #53, #54).
- Read-only live display panel (`flapsim panel`, **experimental**) and its restyle (#49, #56).
- Per-module fault injection in the hardware model (#47).
- Factory-fresh first-boot scenarios, a settings-race stress scenario and a long time-mode soak
  (#44, #46).
- Multi-arch (amd64 and arm64) image publication with an optional Docker Hub mirror (#48).
- Owner-approval gate for releases through the `release` environment (#42).
- Link check for relative Markdown links, run in Fast checks (#71), and a CI-run Mosquitto round-trip test that fails if it would skip (#70).
- `CHANGELOG.md` and the stability statement in [docs/stability.md](docs/stability.md).

### Changed

- Long-running scenarios moved from PR checks to a nightly workflow (#68).
- Docs reorganised: process and planning docs moved to `docs/history/`, a two-part index in
  `docs/README.md` separates using from maintaining flapsim, README compares QEMU and native modes,
  and the release steps include the version bump (#41, #45, #55, #66, #67, #71).

### Fixed

- Two golden-baseline gaps that made container smoke runs flaky (#50).

### Removed

- The frozen proof-of-concept `poc/` directory; it remains in git history (#69).

## [0.1.0]

First published `flapsim` image (#39), released as the `v0.1.0` tag (`3ba32a9`).

### Added

- QEMU mode: pinned QEMU build, the production hardware model for both board layouts, the
  supervised `flapsim run` CLI, the scenario engine with reports and replay, the container image
  with verified publication, and the strict same-run firmware release gate (#9, #10, #11, #12, #13,
  #17).
- Native mode: build the firmware source for Linux against host shims and run scenarios with
  `flapsim run --mode native`, including sockets, the HTTP transport, MQTT/Home Assistant and
  controlled-clock scenarios, and a native job in the container image (#27, #30, #31, #32).
- Control API (**experimental**): authenticated live physical state and audited mechanics edits,
  with the cross-backend research plan (#33, #34, #36).
- Realistic drum mechanics: gearbox ratios, module variance, fractional flap landings, per-module
  sensor geometry and firmware calibration verification (#25, #29).

### Changed

- Golden firmware pinned to v1.4.1; the rc.1 version exception is retired (#26).
- The image is published as `ghcr.io/drewferg11/split-flap-emulator` (#28).
- The known-issues baseline records further v1.4.1 firmware failure forms (#26, #35, #38).
