# Hardware descriptions

`esp32-8-single.yaml` and `esp32-16-dual.yaml` describe physical wiring, drums,
hall sensors and reproducible start angles independently of firmware settings.
Both use seed 7. See [the hardware/schema guide](../docs/hardware-model.md)
for validation, module ordering, random/explicit positions and normalized JSON.

These two original configs are idealized 2048-step references. For realistic
28BYJ-48 gearbox drift and seeded per-module magnet/flap variation, use
`esp32-8-single-realistic.yaml` or `esp32-16-dual-realistic.yaml` (2038 ±2 full
steps per rotation). The firmware still uses its own independent settings.
