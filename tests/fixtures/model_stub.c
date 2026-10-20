/* SPDX-License-Identifier: GPL-2.0-only */
/* Loader/lifecycle tests use a test double; production always links model/adapter.c. */
#include "callbacks.h"

void flapsim_model_prepare(void *library) { (void)library; }
void flapsim_model_start(void) {}
void flapsim_model_finish(void) {}
int flapsim_i2c_event(uint8_t bus, uint8_t addr, uint16_t event) {
    (void)event;
    return bus == 0 && addr == 0x20 ? 0 : 1;
}
