# Scenarios

- `boot-esp32-8-single.yaml` / `boot-esp32-16-dual.yaml`: bounded startup check.
- `release-smoke-8.yaml`: startup, version, single-bus configuration, real reboot,
  persisted settings, WIFI ERR and centered HELLO.
- `release-smoke-16.yaml`: startup and centered dual-row HELLO / WORLD, including
  an explicit idempotent repeat.
- `homing-random.yaml`: recorded seeded start, historical blank homing settlement,
  then X startup; CI runs seeds 7, 19 and 42 independently.
- `version-check.yaml`: strict serial version expectation, also used by the
  asserted negative integration checks.

See [scenario schema, freshness and reports](../docs/scenarios.md). Step-based
scenarios that assert a version require `--var FIRMWARE_VERSION=<release-tag>`.
The published v1.4.0 golden asset also requires the explicit checksum-guarded
`--golden-version-exception`; new releases use strict matching by default.
