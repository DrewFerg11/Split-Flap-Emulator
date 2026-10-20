# Hardware model

Phase 2 introduces the production PCF8575, stepper, drum and bus model here.
Phase 1's bounded integration probe is in `qemu/boot_stub.c`; it only ACKs
bus 0 address 0x20 and cannot simulate homing or display output.
