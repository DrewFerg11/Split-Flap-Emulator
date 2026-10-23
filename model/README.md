# Hardware model

The production core is split into `bus.c`, `pcf8575.c`, `stepper.c`, `drum.c`,
`config.c`, `faults.c` (injected faults, #19) and `io.c`. `adapter.c` connects it to the pinned unmodified QEMU
library using that checkout's configured timer/reset headers.

Configuration, hardware semantics, output schema and reproduction commands:
[`docs/hardware-model.md`](../docs/hardware-model.md).

Run the native core tests without QEMU using `python -m pytest tests/test_model.py`.
