/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef FLAPSIM_INTERNAL_H
#define FLAPSIM_INTERNAL_H
#include "model.h"
#include <json.h>
#include <stdbool.h>
#include <stdio.h>

#define FS_MAX_MODULES 16
#define FS_TICK_NS 100000000LL
typedef struct {
    uint64_t forward, backward, stalls, writes, reads, anomalies;
} FsCounters;
typedef struct {
    int bus, addr, angle, phase, steps, magnet, flap_offset;
    bool energized;
    uint16_t latch;
    FsCounters counters;
} FsModule;
typedef struct {
    int selected, direction, writes, reads;
    uint8_t bytes[2];
    uint16_t input;
} FsBus;
typedef struct {
    FsModule modules[FS_MAX_MODULES];
    int count, steps, magnet, window, nflaps;
    char flaps[96];
    FsBus buses[2];
    FsCounters totals;
    bool initialized, failed, pending_anomaly;
    uint64_t movement_id, settlement_id, sequence;
    int64_t now, last_snapshot;
    char *state_path, *activity_path;
    FILE *timeline, *trace;
    char error[256];
} FsModel;
extern FsModel fs;

void fs_fail(const char *format, ...);
void fs_anomaly(FsModule *module, const char *reason);
bool fs_any_energized(void);
int fs_find(int bus, int address);
int fs_hall(const FsModule *module);
char fs_shown(const FsModule *module);
double fs_flap_position(const FsModule *module);
void fs_stepper_write(FsModule *module, uint16_t output);
void fs_pcf_finish(FsBus *bus, FsModule *module);
uint8_t fs_pcf_read(FsBus *bus);
void fs_emit(const char *event, bool state);
void fs_write_activity(void);

#endif
