# flapsim documentation

The docs are split in two parts by audience. The files stay in this folder, so links between them are unchanged.

## Using flapsim

For people who run the emulator, write scenarios or test firmware with it.

- [Runner](runner.md): the `flapsim run` CLI, outputs and exit codes.
- [Scenarios](scenarios.md): scenario schema, built-in scenarios and known-issue baselines.
- [Stability and versioning](stability.md): what is stable and what is experimental in 1.0.
- [Hardware model](hardware-model.md): hardware configuration and the state/timeline schemas.
- [Calibration](calibration.md): per-module physical calibration and firmware verification.
- [Container](container.md): running the published image.
- [Native-mode checks in firmware CI](firmware-ci.md) and its [example workflow](firmware-native-ci.yml).
- Experimental in 1.0: [live control API](control-api.md) and [live display panel](panel.md). They stabilize in 1.x.

## Maintaining flapsim

For people who build, change or release the emulator.

- [Development](development.md): building, checks, boot reproduction and emulation pitfalls.
- [Maintenance](maintenance.md): QEMU and golden firmware updates, publication procedures.
- [Release gate](release-gate.md): the firmware release gate.
- [Changelog](../CHANGELOG.md) and the release steps in [Maintenance](maintenance.md#release-approval).
- [Native mode internals](native.md) and [mechanics findings](mechanics-findings.md).
- [Architecture decisions](adr): [0001 QEMU integration](adr/0001-qemu-integration.md), [0002 native mode](adr/0002-native-mode.md).
- [Control API plan](control-api-plan.md): working plan for the control API.
- [History](history/README.md): retired process and planning docs.
