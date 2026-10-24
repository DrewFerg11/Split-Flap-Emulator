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
/* Injected faults (#19); all zero for a healthy module. */
enum {
    FS_SENSOR_OK,
    FS_SENSOR_STUCK_ACTIVE,
    FS_SENSOR_STUCK_INACTIVE,
    FS_SENSOR_INVERTED,
    FS_SENSOR_NOISY
};
enum { FS_MOTOR_OK, FS_MOTOR_DEAD, FS_MOTOR_SLIP };
enum { FS_I2C_OK, FS_I2C_MISSING, FS_I2C_DISAPPEAR, FS_I2C_NACK, FS_I2C_CORRUPT };
/* Electrical faults (#64): rail lost, a timed drop window, momentary module brownouts and
 * momentary controller supply sags that abort a transaction. */
enum { FS_POWER_OK, FS_POWER_LOST, FS_POWER_DROP, FS_POWER_BROWNOUT, FS_POWER_SAG };
typedef struct {
    bool configured;
    int sensor, motor, i2c, power;
    double sensor_noise, slip, nack, corrupt, brownout, sag;
    int64_t disappear_at_ns, power_off_at_ns, power_on_at_ns;
    uint32_t seed, rng;
    uint64_t events;
} FsFaults;
typedef struct {
    int bus, addr, angle, phase, steps, magnet, flap_offset, window;
    bool energized, active_low;
    uint16_t latch;
    bool observed_nack, observed_block, observed_power;
    bool powered; /* false while the module rail is down; the model starts powered */
    uint64_t blocked_steps, nacks, power_losses;
    FsCounters counters;
    FsFaults faults;
} FsModule;
typedef struct {
    int selected, direction, writes, reads;
    bool active, rejected, aborted;
    uint8_t bytes[2];
    uint16_t input;
    int hidden; /* address of a present module answering this transaction as absent, or -1 */
} FsBus;
typedef struct {
    FsModule modules[FS_MAX_MODULES], initial_modules[FS_MAX_MODULES];
    int count, steps, magnet, window, nflaps;
    char flaps[96];
    FsBus buses[2];
    FsCounters totals;
    bool initialized, failed, pending_anomaly, faults_enabled, fault_event_pending;
    uint64_t movement_id, settlement_id, sequence;
    uint64_t guest_resets, mechanics_revision;
    struct json_object *operation;
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
struct json_object *fs_mechanics(const FsModule *module);
struct json_object *fs_initial_mechanics(void);
bool fs_parse_mechanics(struct json_object *object, FsModule *module, bool partial);
struct json_object *fs_faults(const FsModule *module);
struct json_object *fs_initial_faults(void);
bool fs_patch_faults(struct json_object *object, FsModule *module);
bool fs_load_fault_state(struct json_object *object, FsModule *module);
bool fs_restore_faults(struct json_object *record);
bool fs_restore_overlay(struct json_object *record);
char *fs_control_command(const char *request, int64_t guest_ns);
bool fs_restore(const char *timeline_path, bool *recorded_reset);
bool fs_parse_faults(struct json_object *entry, FsModule *module);
bool fs_fault_absent(FsModule *module, int64_t guest_ns);
bool fs_fault_sag(FsModule *module);
void fs_power_sync(FsModule *module, int64_t guest_ns, bool emit);
bool fs_power_evidence(const FsModule *module);
int fs_fault_hall(FsModule *module);
bool fs_fault_blocks_step(FsModule *module);
uint8_t fs_fault_read(FsModule *module, uint8_t byte);
struct json_object *fs_faults_json(const FsModule *module);
struct json_object *fs_fault_summary(void);
bool fs_validate_fault_summary(struct json_object *record, bool legacy);

void fs_telemetry_start(void);
void fs_telemetry_event(uint8_t bus, uint8_t address, uint16_t event, int result, int64_t guest_ns);
void fs_telemetry_boundary(int kind); /* wire kinds: 2 reset, 3 power-off */
void fs_telemetry_state(int64_t guest_ns);
void fs_telemetry_close(void);

#endif
