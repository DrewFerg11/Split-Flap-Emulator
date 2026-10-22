/* SPDX-License-Identifier: GPL-2.0-only */
/* Injected per-module faults (#19). Randomness comes from one xorshift32 stream per module,
 * seeded by the normalized config and advanced only by firmware transactions, so a run with
 * the same seed and firmware traffic injects the same faults and snapshots never perturb it.
 * A resumed process restarts each stream from its seed, like the rest of the configuration. */
#include "internal.h"
#include <string.h>

static const char *const SENSOR[] = {"ok", "stuck_active", "stuck_inactive", "inverted", "noisy"};
static const char *const MOTOR[] = {"ok", "dead", "slip"};
static const char *const I2C[] = {"ok", "missing", "disappear", "nack", "corrupt"};

static uint32_t next(FsFaults *faults) {
    uint32_t value = faults->rng;
    value ^= value << 13;
    value ^= value >> 17;
    value ^= value << 5;
    return faults->rng = value;
}

static bool chance(FsFaults *faults, double probability) {
    return next(faults) < probability * 4294967296.0;
}

static int choice(struct json_object *faults, const char *key, const char *const *names,
                  int count) {
    struct json_object *value;
    if (!json_object_object_get_ex(faults, key, &value))
        return 0;
    if (json_object_is_type(value, json_type_string)) {
        const char *text = json_object_get_string(value);
        for (int i = 1; i < count; i++) {
            if (!strcmp(text, names[i]))
                return i;
        }
    }
    fs_fail("normalized faults.%s is invalid", key);
    return 0;
}

static double probability(struct json_object *faults, const char *key, bool required) {
    struct json_object *value;
    if (!json_object_object_get_ex(faults, key, &value)) {
        if (required)
            fs_fail("normalized faults.%s is required", key);
        return 0;
    }
    double number = json_object_is_type(value, json_type_double) ? json_object_get_double(value)
                    : json_object_is_type(value, json_type_int)
                        ? (double)json_object_get_int64(value)
                        : -1;
    if (!required || !(number > 0 && number <= 1)) {
        fs_fail("normalized faults.%s must be a probability in (0, 1] for its fault", key);
        return 0;
    }
    return number;
}

bool fs_parse_faults(struct json_object *entry, FsModule *module) {
    struct json_object *faults, *value;
    memset(&module->faults, 0, sizeof(module->faults));
    if (!json_object_object_get_ex(entry, "faults", &faults))
        return true;
    if (!json_object_is_type(faults, json_type_object)) {
        fs_fail("normalized faults must be an object");
        return false;
    }
    static const char *const KEYS[] = {"sensor", "motor",   "i2c",  "sensor_noise",   "slip",
                                       "nack",   "corrupt", "seed", "disappear_at_ns"};
    json_object_object_foreach(faults, key, unused) {
        (void)unused;
        size_t i = 0;
        while (i < sizeof(KEYS) / sizeof(*KEYS) && strcmp(key, KEYS[i]))
            i++;
        if (i == sizeof(KEYS) / sizeof(*KEYS)) {
            fs_fail("normalized faults.%s is not a fault parameter", key);
            return false;
        }
    }
    FsFaults *f = &module->faults;
    f->configured = true;
    f->sensor = choice(faults, "sensor", SENSOR, 5);
    f->motor = choice(faults, "motor", MOTOR, 3);
    f->i2c = choice(faults, "i2c", I2C, 5);
    f->sensor_noise = probability(faults, "sensor_noise", f->sensor == FS_SENSOR_NOISY);
    f->slip = probability(faults, "slip", f->motor == FS_MOTOR_SLIP);
    f->nack = probability(faults, "nack", f->i2c == FS_I2C_NACK);
    f->corrupt = probability(faults, "corrupt", f->i2c == FS_I2C_CORRUPT);
    if (json_object_object_get_ex(faults, "disappear_at_ns", &value) !=
        (f->i2c == FS_I2C_DISAPPEAR)) {
        fs_fail("normalized faults.disappear_at_ns belongs to i2c: disappear");
    } else if (f->i2c == FS_I2C_DISAPPEAR) {
        if (!json_object_is_type(value, json_type_int) || json_object_get_int64(value) <= 0)
            fs_fail("normalized faults.disappear_at_ns must be a positive integer");
        else
            f->disappear_at_ns = json_object_get_int64(value);
    }
    if (!json_object_object_get_ex(faults, "seed", &value) ||
        !json_object_is_type(value, json_type_int) || json_object_get_int64(value) <= 0 ||
        json_object_get_int64(value) > 0xffffffffLL)
        fs_fail("normalized faults.seed must be a nonzero 32-bit integer");
    else
        f->rng = (uint32_t)json_object_get_int64(value);
    if (!f->sensor && !f->motor && !f->i2c)
        fs_fail("normalized faults name no fault");
    return !fs.failed;
}

bool fs_fault_absent(FsModule *module, int64_t guest_ns) {
    FsFaults *f = &module->faults;
    bool absent = f->i2c == FS_I2C_MISSING ||
                  (f->i2c == FS_I2C_DISAPPEAR && guest_ns >= f->disappear_at_ns) ||
                  (f->i2c == FS_I2C_NACK && chance(f, f->nack));
    if (absent)
        f->events++;
    return absent;
}

/* The level the firmware reads; snapshots keep reporting the physical sensor. */
int fs_fault_hall(FsModule *module) {
    FsFaults *f = &module->faults;
    int physical = fs_hall(module);
    int level = physical;
    switch (f->sensor) {
    case FS_SENSOR_STUCK_ACTIVE:
        level = !module->active_low;
        break;
    case FS_SENSOR_STUCK_INACTIVE:
        level = module->active_low;
        break;
    case FS_SENSOR_INVERTED:
        level = !physical;
        break;
    case FS_SENSOR_NOISY:
        if (chance(f, f->sensor_noise))
            level = !physical;
        break;
    default:
        break;
    }
    if (level != physical)
        f->events++;
    return level;
}

bool fs_fault_blocks_step(FsModule *module) {
    FsFaults *f = &module->faults;
    bool blocked = f->motor == FS_MOTOR_DEAD || (f->motor == FS_MOTOR_SLIP && chance(f, f->slip));
    if (blocked)
        f->events++;
    return blocked;
}

uint8_t fs_fault_read(FsModule *module, uint8_t byte) {
    FsFaults *f = &module->faults;
    if (f->i2c != FS_I2C_CORRUPT || !chance(f, f->corrupt))
        return byte;
    f->events++;
    return byte ^ (uint8_t)(1u << (next(f) & 7));
}

struct json_object *fs_faults_json(const FsModule *module) {
    const FsFaults *f = &module->faults;
    struct json_object *object = json_object_new_object();
    if (f->sensor)
        json_object_object_add(object, "sensor", json_object_new_string(SENSOR[f->sensor]));
    if (f->motor)
        json_object_object_add(object, "motor", json_object_new_string(MOTOR[f->motor]));
    if (f->i2c)
        json_object_object_add(object, "i2c", json_object_new_string(I2C[f->i2c]));
    json_object_object_add(object, "events", json_object_new_int64((int64_t)f->events));
    return object;
}
