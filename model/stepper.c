/* SPDX-License-Identifier: GPL-2.0-only */
#include "internal.h"

void fs_stepper_write(FsModule *module, uint16_t output) {
    int coils = (output >> 1) & 15;
    int phase;
    switch (coils) {
    case 3:
        phase = 0;
        break;
    case 9:
        phase = 1;
        break;
    case 12:
        phase = 2;
        break;
    case 6:
        phase = 3;
        break;
    default:
        phase = -1;
        break;
    }
    module->latch = output;
    module->counters.writes++;
    module->energized = coils != 0;
    if (phase < 0) {
        if (coils)
            fs_anomaly(module, "invalid coil pattern");
        module->phase = -1;
        return;
    }
    if (module->phase >= 0) {
        int delta = (phase - module->phase + 4) % 4;
        if ((delta == 1 || delta == 3) && module->dead_motor) {
            module->blocked_steps++;
            if (!module->observed_block) {
                module->observed_block = true;
                fs.fault_event_pending = true;
            }
        } else if (delta == 1) {
            module->angle = (module->angle + 1) % module->steps;
            module->counters.forward++;
        } else if (delta == 3) {
            module->angle = (module->angle + module->steps - 1) % module->steps;
            module->counters.backward++;
        } else if (delta == 2) {
            module->counters.stalls++;
        }
    }
    /* The first energize establishes phase without inventing a full step. */
    module->phase = phase;
}
