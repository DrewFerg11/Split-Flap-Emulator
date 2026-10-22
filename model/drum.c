/* SPDX-License-Identifier: GPL-2.0-only */
#include "internal.h"

int fs_hall(const FsModule *module) {
    bool present =
        (module->angle - module->magnet + module->steps) % module->steps < module->window;
    if (module->hall_fault)
        present = module->hall_fault == 1;
    return present != module->active_low;
}

double fs_flap_position(const FsModule *module) {
    int angle = (module->angle - module->flap_offset + module->steps) % module->steps;
    return (double)angle * fs.nflaps / module->steps;
}

char fs_shown(const FsModule *module) {
    int angle = (module->angle - module->flap_offset + module->steps) % module->steps;
    int index = ((int64_t)angle * fs.nflaps * 2 + module->steps) / (2LL * module->steps);
    return fs.flaps[index % fs.nflaps];
}
