# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/) as described in [docs/stability.md](docs/stability.md).

## [Unreleased]

Everything below is merged to `main` since 0.1.0 and is planned for 1.0.0. The version
number in `pyproject.toml` is still `0.1.0`; it changes in the release PR.

### Added

- Native mode: build the firmware source for Linux against host shims and run scenarios with
  `flapsim run --mode native`, including sockets, the HTTP transport, MQTT/Home Assistant and
  controlled-clock scenarios, and a native job in the container image (#27, #30, #31, #32).
- Live control API (**experimental**): authenticated live physical state, audited mechanics edits,
  controller reset and power off/on, native runtime speed and runtime fault controls (#34, #36,
  #37, #43, #51, #53, #54).
- Read-only live display panel (`flapsim panel`, **experimental**) and its restyle (#49, #56).
- Per-module fault injection in the hardware model (#47).
- Realistic drum mechanics: gearbox ratios, module variance, fractional flap landings, per-module
  sensor geometry and firmware calibration verification (#25, #29).
- Factory-fresh first-boot scenarios, a settings-race stress scenario and a long time-mode soak
  (#44, #46).
- Multi-arch (amd64 and arm64) image publication with an optional Docker Hub mirror (#48).
- Owner-approval gate for releases through the `release` environment (#42).
- Link check for relative Markdown links, run in Fast checks.
- `CHANGELOG.md` and the stability statement in [docs/stability.md](docs/stability.md).

### Changed

- Golden firmware pinned to v1.4.1; the rc.1 version exception is retired (#26).
- Long-running scenarios moved from PR checks to a nightly workflow (#68).
- Docs reorganised: process and planning docs moved to `docs/history/`, a two-part index in
  `docs/README.md` separates using from maintaining flapsim, and README compares QEMU and native
  modes (#55, #66, #71), and the release steps now include the version bump (#67).
- The image is published as `ghcr.io/drewferg11/split-flap-emulator` (#28).

### Fixed

- Two golden-baseline gaps that made container smoke runs flaky (#50).
- The known-issues baseline records further v1.4.1 firmware failure forms and the logged I2C write
  timeout case (#35, #38).

### Removed

- The frozen proof-of-concept `poc/` directory; it remains in git history (#69).

## [0.1.0]

First published `flapsim` image (#39): QEMU-mode runner with the production hardware model,
scenario engine, reports and replay, the container image and the strict firmware release gate.
