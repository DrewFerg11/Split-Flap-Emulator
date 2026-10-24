/* SPDX-License-Identifier: GPL-2.0-only */
#include "internal.h"
#include <string.h>

FsModel fs;

int fs_find(int bus, int address) {
    for (int i = 0; i < fs.count; i++) {
        if (fs.modules[i].bus == bus && fs.modules[i].addr == address)
            return i;
    }
    return -1;
}

bool fs_any_energized(void) {
    for (int i = 0; i < fs.count; i++) {
        if (fs.modules[i].energized)
            return true;
    }
    return false;
}

void fs_anomaly(FsModule *module, const char *reason) {
    module->counters.anomalies++;
    fs.totals.anomalies++;
    fprintf(stderr, "flapsim: anomaly bus=%d addr=0x%02x: %s\n", module->bus, module->addr, reason);
    fs.pending_anomaly = true;
}

static int event_inner(uint8_t id, uint8_t address, uint16_t event, int64_t guest_ns) {
    if (!fs.initialized || fs.failed || id > 1)
        return 1;
    fs.now = guest_ns;
    int index = fs_find(id, address);
    int kind = event & 255;
    if (fs.trace) {
        fprintf(fs.trace, "%lld bus=%u addr=0x%02x event=0x%04x %s\n", (long long)guest_ns, id,
                address, event, index >= 0 ? "present" : "absent");
        fflush(fs.trace);
    }
    FsBus *bus = &fs.buses[id];
    if (kind <= 2) {
        if (bus->selected >= 0)
            fs_anomaly(&fs.modules[bus->selected], "START while transaction open");
        memset(bus, 0, sizeof(*bus));
        bus->hidden = -1;
        /* An injected I2C fault answers the whole transaction as an absent address. */
        if (index >= 0 && fs_fault_absent(&fs.modules[index], guest_ns)) {
            FsModule *module = &fs.modules[index];
            module->nacks++;
            if (!module->observed_nack) {
                module->observed_nack = true;
                fs.fault_event_pending = true;
            }
            bus->hidden = address;
            index = -1;
        }
        bus->selected = index;
        bus->active = index >= 0;
        bus->rejected = index < 0;
        bus->direction = kind;
        if (index >= 0) {
            FsModule *module = &fs.modules[index];
            /* Only a read samples the sensor, so writes never consume or count sensor faults. */
            int hall = kind == 0 ? fs_fault_hall(module) : fs_hall(module);
            bus->input = (module->latch & 0x7fff) | (hall ? 0x8000 : 0);
            bus->aborted = fs_fault_sag(module);
        }
        return index >= 0 ? 0 : 1;
    }
    if (kind == 3) {
        bus->active = false;
        /* A NACKed address has no latched transaction. Its eventual STOP may
         * arrive after the module has been restored through the control API. */
        if (bus->rejected) {
            bus->rejected = false;
            return 1;
        }
    }
    if (bus->hidden == address)
        index = -1;
    if (index < 0)
        return kind == 6 ? 0xff : 1;
    FsModule *module = &fs.modules[index];
    if (kind == 4)
        return 0; /* Master's NACK after reading is normal. */
    if (bus->selected != index) {
        fs_anomaly(module, "data or FINISH without matching START");
        return 1;
    }
    if (kind == 3) {
        bool moving = fs_any_energized();
        if (!bus->aborted)
            fs_pcf_finish(bus, module);
        bus->selected = -1;
        bool now_moving = fs_any_energized();
        if (!moving && now_moving) {
            fs.movement_id++;
            fs_emit("move_started", false);
        } else if (moving && !now_moving) {
            fs.settlement_id++;
            fs_emit("move_finished", true);
        }
        return 0;
    }
    if (kind == 5 && bus->direction != 0) {
        if (bus->aborted)
            return 0;
        if (bus->writes < 2)
            bus->bytes[bus->writes] = event >> 8;
        if (bus->writes < 3)
            bus->writes++;
        return 0;
    }
    if (kind == 6 && bus->direction == 0)
        return bus->aborted ? 0xff : fs_fault_read(module, fs_pcf_read(bus));
    fs_anomaly(module, "invalid event or transaction direction");
    return 1;
}

int fs_model_event(uint8_t id, uint8_t address, uint16_t event, int64_t guest_ns) {
    int result = event_inner(id, address, event, guest_ns);
    fs_telemetry_event(id, address, event, result, guest_ns);
    if (fs.fault_event_pending) {
        fs.fault_event_pending = false;
        fs_emit("fault_observed", !fs_any_energized());
    }
    if (fs.pending_anomaly) {
        fs.pending_anomaly = false;
        fs_emit("anomaly", false);
    }
    return result;
}

void fs_model_tick(int64_t guest_ns) {
    if (!fs.initialized || fs.failed)
        return;
    fs.now = guest_ns;
    /* Timed rail changes apply even when the firmware is quiet, but never inside a transaction. */
    for (int i = 0; i < fs.count; i++) {
        if (fs.modules[i].faults.power && fs.buses[fs.modules[i].bus].selected != i)
            fs_power_sync(&fs.modules[i], guest_ns, true);
    }
    fs_telemetry_state(guest_ns);
    if (fs_any_energized() && guest_ns - fs.last_snapshot >= FS_TICK_NS) {
        fs.last_snapshot = guest_ns;
        fs_emit("snapshot", false);
    } else {
        fs_write_activity();
    }
}

void fs_model_guest_reset(void) {
    if (!fs.initialized || fs.failed)
        return;
    fs.guest_resets++;
    fs_telemetry_boundary(2);
    for (int b = 0; b < 2; b++) {
        memset(&fs.buses[b], 0, sizeof(fs.buses[b]));
        fs.buses[b].selected = fs.buses[b].hidden = -1;
    }
    /* The ESP32/controller resets; external expander latches and drums do not. */
    fs_emit("guest_reset", false);
}
