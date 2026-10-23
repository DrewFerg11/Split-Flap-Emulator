# Scenarios

- `boot-esp32-8-single.yaml` / `boot-esp32-16-dual.yaml`: bounded startup check.
- `release-smoke-8.yaml`: startup, version, single-bus configuration, real reboot,
  persisted settings, WIFI ERR and centered HELLO.
- `release-smoke-16.yaml`: startup and centered dual-row HELLO / WORLD, including
  an explicit idempotent repeat.
- `homing-random.yaml`: recorded seeded start, historical blank homing settlement,
  then X startup; PRs run seed 7; the nightly workflow runs seeds 19 and 42.
- `mqtt-native-8.yaml` (native only): provisioned Wi-Fi and broker; Home Assistant
  discovery, availability, MQTT `set` → drums and state publishing.
- `clock-dst-native-8.yaml` / `date-rollover-native-8.yaml` (native only): time mode
  across a DST transition and date mode across New Year with a fixed wall clock.
- `version-check.yaml`: strict serial version expectation, also used by the
  asserted negative integration checks.
- `calibration-8.yaml`: save eight different offsets through `POST /settings`,
  observe the real reboot, and assert all eight physical drums land centered.
  Pair with `esp32-8-single-calibration`; see [calibration.md](../docs/calibration.md).

See [scenario schema, freshness and reports](../docs/scenarios.md). Step-based
scenarios that assert a version require `--var FIRMWARE_VERSION=<release-tag>`.
Version matching is always strict.
Golden CI runs also pass `--known-issues baselines/v1.4.1-golden.yaml`, a
checksum-bound list of linked known firmware defects (see #15 and the docs).
