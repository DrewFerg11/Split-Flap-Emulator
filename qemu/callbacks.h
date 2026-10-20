/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef FLAPSIM_CALLBACKS_H
#define FLAPSIM_CALLBACKS_H

#include <stdint.h>

/* ABI copied from hw/xtensa/esp32_picsimlab.c at QEMU_COMMIT. Check on pin bumps. */
typedef struct {
    void (*write_pin)(int pin, int value);
    void (*dir_pin)(int pin, int value);
    int (*i2c_event)(uint8_t id, uint8_t addr, uint16_t event);
    uint8_t (*spi_event)(uint8_t id, uint16_t event);
    void (*uart_tx_event)(uint8_t id, uint8_t value);
    const short *pinmap;
    void (*rmt_event)(uint8_t channel, uint32_t config0, uint32_t value);
} flapsim_callbacks;

int flapsim_i2c_event(uint8_t id, uint8_t addr, uint16_t event);

#endif
