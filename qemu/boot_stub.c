/* SPDX-License-Identifier: GPL-2.0-only */
/* Temporary phase-1 probe. Phase 2 replaces it with the production model. */
#include "callbacks.h"
#include <stdio.h>

int flapsim_i2c_event(uint8_t id, uint8_t addr, uint16_t event) {
    static int reported;
    if (id != 0 || addr != 0x20) {
        return -1;
    }
    if (!reported) {
        fprintf(stderr, "FLAPSIM probe: bus=0 address=0x20 ACK event=%u\n", event);
        fflush(stderr);
        reported = 1;
    }
    /* I2C_NACK=4, data-write=5, data-read=6 at the pinned fork. */
    return (event & 0xff) == 6 ? 0xff : 0;
}
