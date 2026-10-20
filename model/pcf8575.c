/* SPDX-License-Identifier: GPL-2.0-only */
#include "internal.h"

void fs_pcf_finish(FsBus *bus, FsModule *module) {
    if (bus->writes && bus->writes != 2)
        fs_anomaly(module, "write length must be two bytes");
    if (bus->reads && bus->reads != 2)
        fs_anomaly(module, "read length must be two bytes");
    if (bus->writes == 2) {
        fs_stepper_write(module, (uint16_t)bus->bytes[0] | ((uint16_t)bus->bytes[1] << 8));
    }
    if (bus->reads == 2)
        module->counters.reads++;
}

uint8_t fs_pcf_read(FsBus *bus) {
    int byte = bus->reads;
    if (bus->reads < 3)
        bus->reads++;
    return byte == 0 ? bus->input & 255 : bus->input >> 8;
}
