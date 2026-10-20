/* SPDX-License-Identifier: GPL-2.0-only */
#include "internal.h"

int fs_hall(const FsModule *module) {
    return (module->angle - fs.magnet + fs.steps) % fs.steps < fs.window;
}

char fs_shown(const FsModule *module) {
    int index = ((int64_t)module->angle * fs.nflaps * 2 + fs.steps) / (2LL * fs.steps);
    return fs.flaps[index % fs.nflaps];
}
