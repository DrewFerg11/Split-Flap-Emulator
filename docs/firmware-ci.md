# Native-mode checks in Split-Flap Display CI

Plan step 6 of [#24](https://github.com/DrewFerg11/Split-Flap-Emulator/issues/24). The firmware
repository adds one **non-gating** job that builds each pull request's own source in native mode
and runs the emulator's scenarios against it. QEMU mode against the released image stays the
release gate. Make the job required once it has been stable on firmware PRs for a while.

Copy [`firmware-native-ci.yml`](firmware-native-ci.yml) to
`.github/workflows/ci-native-emulator.yml` in Split-Flap-Display. Then set `EMULATOR_REF` to a
merged emulator commit (a full SHA, so the checks change only when you bump it on purpose).

## What it checks

| Scenario | Layout | Covers |
|---|---|---|
| `release-smoke-8` | `esp32-8-single` | boot to X, version banner and `/version`, hardware settings reboot, `WIFI ERR`, `HELLO` |
| `release-smoke-16` | `esp32-16-dual` | dual-bus boot, `dual-single` HELLO / WORLD, idempotent repeat |
| `mqtt-native-8` | `esp32-8-single` | Wi-Fi + broker from seeded settings, Home Assistant discovery, MQTT `set` → drums, state |
| `clock-dst-native-8` | `esp32-8-single` | time mode in Central European Time across spring-forward |
| `date-rollover-native-8` | `esp32-8-single` | date mode across New Year |

Runs are **strict**: no known-issues baseline applies to PR source (baselines bind only the
clean golden commit). The expected version is whatever the firmware's own `inject_version.py`
rule produces for the PR checkout, read back from `build-info.json`. Every scenario runs even
if an earlier one fails. The job summary lists each outcome, and the reports, JUnit, replays
and serial logs are uploaded as artifacts.

## Expected state today

The released v1.4.1 source fails the strict smoke scenarios on
[SFD#54](https://github.com/DrewFerg11/Split-Flap-Display/issues/54) (settings lost to the
loop/web-server `Preferences` race, plus `NOT_FOUND` noise for unwritten keys). Native mode
reproduces these far more reliably than QEMU mode. A PR that fixes #54 should turn the smoke
scenarios green, which is why the job helps verify that fix. The MQTT and clock scenarios
fail only on the `NOT_FOUND` noise until then.
