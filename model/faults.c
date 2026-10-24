/* SPDX-License-Identifier: GPL-2.0-only */
/* Injected per-module faults (#19). Randomness comes from one xorshift32 stream per module,
 * seeded by the normalized config and advanced only by firmware transactions, so a run with
 * the same seed and firmware traffic injects the same faults and snapshots never perturb it.
 * Versioned runtime evidence preserves the stream and effect counters across resume. */
#include "internal.h"
#include <string.h>

static const char *const SENSOR[] = {"ok", "stuck_active", "stuck_inactive", "inverted", "noisy"};
static const char *const MOTOR[] = {"ok", "dead", "slip"};
static const char *const I2C[] = {"ok", "missing", "disappear", "nack", "corrupt"};
static const char *const POWER[] = {"ok", "lost", "drop", "brownout", "sag"};

/* PCF8575 power-on reset drives every output high; an unpowered module drives nothing. */
#define POWER_ON_LATCH 0xffff
#define POWER_OFF_LATCH 0

/* Resolved configuration; unlike the legacy summary, this binds parameters and seed. */
struct json_object *fs_faults(const FsModule *module) {
    const FsFaults *f = &module->faults;
    struct json_object *object = json_object_new_object();
    json_object_object_add(object, "sensor", json_object_new_string(SENSOR[f->sensor]));
    json_object_object_add(object, "motor", json_object_new_string(MOTOR[f->motor]));
    json_object_object_add(object, "i2c", json_object_new_string(I2C[f->i2c]));
    /* Healthy-power modules keep the pre-#64 shape so older evidence stays valid. */
    if (f->power)
        json_object_object_add(object, "power", json_object_new_string(POWER[f->power]));
    json_object_object_add(object, "seed", json_object_new_int64(f->seed));
    json_object_object_add(object, "configured", json_object_new_boolean(f->configured));
    if (f->sensor == FS_SENSOR_NOISY)
        json_object_object_add(object, "sensor_noise", json_object_new_double(f->sensor_noise));
    if (f->motor == FS_MOTOR_SLIP)
        json_object_object_add(object, "slip", json_object_new_double(f->slip));
    if (f->i2c == FS_I2C_NACK)
        json_object_object_add(object, "nack", json_object_new_double(f->nack));
    if (f->i2c == FS_I2C_CORRUPT)
        json_object_object_add(object, "corrupt", json_object_new_double(f->corrupt));
    if (f->i2c == FS_I2C_DISAPPEAR)
        json_object_object_add(object, "disappear_at_ns",
                               json_object_new_int64(f->disappear_at_ns));
    if (f->power == FS_POWER_BROWNOUT)
        json_object_object_add(object, "brownout", json_object_new_double(f->brownout));
    if (f->power == FS_POWER_SAG)
        json_object_object_add(object, "sag", json_object_new_double(f->sag));
    if (f->power == FS_POWER_DROP) {
        json_object_object_add(object, "power_off_at_ns",
                               json_object_new_int64(f->power_off_at_ns));
        json_object_object_add(object, "power_on_at_ns", json_object_new_int64(f->power_on_at_ns));
    }
    return object;
}

/* Power effects extend the effect counters only once they matter, like the faults object. */
bool fs_power_evidence(const FsModule *module) {
    return module->faults.power || module->power_losses || !module->powered;
}

static int literal(struct json_object *value, const char *const *names, int count) {
    if (!json_object_is_type(value, json_type_string))
        return -1;
    const char *text = json_object_get_string(value);
    if (strlen(text) != (size_t)json_object_get_string_len(value))
        return -1;
    for (int i = 0; i < count; i++)
        if (!strcmp(text, names[i]))
            return i;
    return -1;
}

bool fs_patch_faults(struct json_object *object, FsModule *module) {
    if (!object || !json_object_is_type(object, json_type_object) ||
        !json_object_object_length(object))
        return false;
    FsFaults *f = &module->faults;
    json_object_object_foreach(object, key, value) {
        int mode;
        if (!strcmp(key, "sensor")) {
            if ((mode = literal(value, SENSOR, 3)) < 0)
                return false;
            f->sensor = mode;
            f->sensor_noise = 0;
        } else if (!strcmp(key, "motor")) {
            if ((mode = literal(value, MOTOR, 2)) < 0)
                return false;
            f->motor = mode;
            f->slip = 0;
        } else if (!strcmp(key, "i2c")) {
            if ((mode = literal(value, I2C, 2)) < 0)
                return false;
            f->i2c = mode;
            f->nack = f->corrupt = 0;
            f->disappear_at_ns = 0;
        } else if (!strcmp(key, "power")) {
            /* Only the rail switch is a runtime control; timed and random modes stay startup-only.
             */
            if ((mode = literal(value, POWER, 2)) < 0)
                return false;
            f->power = mode;
            f->brownout = f->sag = 0;
            f->power_off_at_ns = f->power_on_at_ns = 0;
        } else
            return false;
    }
    f->configured = true;
    return true;
}

bool fs_load_fault_state(struct json_object *object, FsModule *module) {
    if (!object || !json_object_is_type(object, json_type_object))
        return false;
    FsFaults restored = {0};
    struct json_object *value;
    const char *keys[] = {"sensor", "motor", "i2c"};
    const char *const *names[] = {SENSOR, MOTOR, I2C};
    int counts[] = {5, 3, 5}, *modes[] = {&restored.sensor, &restored.motor, &restored.i2c};
    for (int i = 0; i < 3; i++) {
        if (!json_object_object_get_ex(object, keys[i], &value) ||
            (*modes[i] = literal(value, names[i], counts[i])) < 0)
            return false;
    }
    /* Absent means healthy; an explicit "ok" is not canonical and fails the final comparison. */
    if (json_object_object_get_ex(object, "power", &value) &&
        (restored.power = literal(value, POWER, 5)) < 0)
        return false;
    if (!json_object_object_get_ex(object, "seed", &value) ||
        !json_object_is_type(value, json_type_int) || json_object_get_int64(value) < 0 ||
        json_object_get_int64(value) > UINT32_MAX)
        return false;
    restored.seed = (uint32_t)json_object_get_int64(value);
    if (!json_object_object_get_ex(object, "configured", &value) ||
        !json_object_is_type(value, json_type_boolean))
        return false;
    restored.configured = json_object_get_boolean(value);
    const FsFaults *initial = &module->faults;
    if (restored.seed != initial->seed || (initial->configured && !restored.configured) ||
        (restored.sensor > FS_SENSOR_STUCK_INACTIVE && restored.sensor != initial->sensor) ||
        (restored.motor > FS_MOTOR_DEAD && restored.motor != initial->motor) ||
        (restored.i2c > FS_I2C_MISSING && restored.i2c != initial->i2c) ||
        (restored.power > FS_POWER_LOST && restored.power != initial->power))
        return false;
    if (!restored.configured &&
        (restored.seed || restored.sensor || restored.motor || restored.i2c || restored.power))
        return false;
    const char *parameters[] = {"sensor_noise", "slip", "nack", "corrupt", "brownout", "sag"};
    bool required[] = {restored.sensor == FS_SENSOR_NOISY,  restored.motor == FS_MOTOR_SLIP,
                       restored.i2c == FS_I2C_NACK,         restored.i2c == FS_I2C_CORRUPT,
                       restored.power == FS_POWER_BROWNOUT, restored.power == FS_POWER_SAG};
    double *numbers[] = {&restored.sensor_noise, &restored.slip,     &restored.nack,
                         &restored.corrupt,      &restored.brownout, &restored.sag};
    double originals[] = {initial->sensor_noise, initial->slip,     initial->nack,
                          initial->corrupt,      initial->brownout, initial->sag};
    for (int i = 0; i < 6; i++) {
        bool present = json_object_object_get_ex(object, parameters[i], &value);
        if (present != required[i])
            return false;
        if (present) {
            if ((!json_object_is_type(value, json_type_double) &&
                 !json_object_is_type(value, json_type_int)) ||
                !restored.seed)
                return false;
            *numbers[i] = json_object_get_double(value);
            if (!(*numbers[i] > 0 && *numbers[i] <= 1) || *numbers[i] != originals[i])
                return false;
        }
    }
    const char *times[] = {"disappear_at_ns", "power_off_at_ns", "power_on_at_ns"};
    bool timed[] = {restored.i2c == FS_I2C_DISAPPEAR, restored.power == FS_POWER_DROP,
                    restored.power == FS_POWER_DROP};
    int64_t *instants[] = {&restored.disappear_at_ns, &restored.power_off_at_ns,
                           &restored.power_on_at_ns};
    int64_t configured[] = {initial->disappear_at_ns, initial->power_off_at_ns,
                            initial->power_on_at_ns};
    for (int i = 0; i < 3; i++) {
        if (json_object_object_get_ex(object, times[i], &value) != timed[i])
            return false;
        if (timed[i]) {
            if (!json_object_is_type(value, json_type_int) || json_object_get_int64(value) <= 0)
                return false;
            *instants[i] = json_object_get_int64(value);
            if (*instants[i] != configured[i])
                return false;
        }
    }
    FsModule copy = *module;
    copy.faults = restored;
    struct json_object *canonical = fs_faults(&copy);
    bool valid = json_object_equal(canonical, object);
    json_object_put(canonical);
    if (valid)
        module->faults = restored;
    return valid;
}

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
    static const char *const KEYS[] = {
        "sensor",          "motor",         "i2c",  "power",
        "sensor_noise",    "slip",          "nack", "corrupt",
        "brownout",        "sag",           "seed", "disappear_at_ns",
        "power_off_at_ns", "power_on_at_ns"};
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
    f->power = choice(faults, "power", POWER, 5);
    f->sensor_noise = probability(faults, "sensor_noise", f->sensor == FS_SENSOR_NOISY);
    f->slip = probability(faults, "slip", f->motor == FS_MOTOR_SLIP);
    f->nack = probability(faults, "nack", f->i2c == FS_I2C_NACK);
    f->corrupt = probability(faults, "corrupt", f->i2c == FS_I2C_CORRUPT);
    f->brownout = probability(faults, "brownout", f->power == FS_POWER_BROWNOUT);
    f->sag = probability(faults, "sag", f->power == FS_POWER_SAG);
    if (json_object_object_get_ex(faults, "disappear_at_ns", &value) !=
        (f->i2c == FS_I2C_DISAPPEAR)) {
        fs_fail("normalized faults.disappear_at_ns belongs to i2c: disappear");
    } else if (f->i2c == FS_I2C_DISAPPEAR) {
        if (!json_object_is_type(value, json_type_int) || json_object_get_int64(value) <= 0)
            fs_fail("normalized faults.disappear_at_ns must be a positive integer");
        else
            f->disappear_at_ns = json_object_get_int64(value);
    }
    bool drop = f->power == FS_POWER_DROP;
    if (json_object_object_get_ex(faults, "power_off_at_ns", &value) != drop ||
        json_object_object_get_ex(faults, "power_on_at_ns", &value) != drop) {
        fs_fail("normalized faults.power_off_at_ns and power_on_at_ns belong to power: drop");
    } else if (drop) {
        struct json_object *off = NULL, *on = NULL;
        json_object_object_get_ex(faults, "power_off_at_ns", &off);
        json_object_object_get_ex(faults, "power_on_at_ns", &on);
        if (!json_object_is_type(off, json_type_int) || !json_object_is_type(on, json_type_int) ||
            json_object_get_int64(off) <= 0 ||
            json_object_get_int64(on) <= json_object_get_int64(off))
            fs_fail("normalized faults.power_on_at_ns must follow a positive power_off_at_ns");
        else {
            f->power_off_at_ns = json_object_get_int64(off);
            f->power_on_at_ns = json_object_get_int64(on);
        }
    }
    if (!json_object_object_get_ex(faults, "seed", &value) ||
        !json_object_is_type(value, json_type_int) || json_object_get_int64(value) <= 0 ||
        json_object_get_int64(value) > 0xffffffffLL)
        fs_fail("normalized faults.seed must be a nonzero 32-bit integer");
    else
        f->seed = f->rng = (uint32_t)json_object_get_int64(value);
    if (!f->sensor && !f->motor && !f->i2c && !f->power)
        fs_fail("normalized faults name no fault");
    return !fs.failed;
}

/* One reset of the module's electrical state. A power change can start or end motion, so it
 * keeps the movement and settlement identities honest; the control path records its own event. */
static void power_reset(FsModule *module, uint16_t latch, bool emit) {
    bool moving = fs_any_energized();
    module->latch = latch;
    module->energized = ((latch >> 1) & 15) != 0;
    module->phase = -1;
    bool now_moving = fs_any_energized();
    if (!moving && now_moving) {
        fs.movement_id++;
        if (emit)
            fs_emit("move_started", false);
    } else if (moving && !now_moving) {
        fs.settlement_id++;
        if (emit)
            fs_emit("move_finished", true);
    }
}

/* The rail the configured mode demands at this instant; brownouts and sags are momentary. */
static bool rail_up(const FsFaults *f, int64_t guest_ns) {
    return f->power != FS_POWER_LOST &&
           !(f->power == FS_POWER_DROP && guest_ns >= f->power_off_at_ns &&
             guest_ns < f->power_on_at_ns);
}

/* Applies a rail transition at most once per change. A module without power drives nothing
 * and its coils release; a powered-up expander starts from its reset state, not its old latch. */
void fs_power_sync(FsModule *module, int64_t guest_ns, bool emit) {
    const FsFaults *f = &module->faults;
    /* A drop is the only way a module with this mode loses its rail, so a missing loss means
     * the window opened since anyone looked, even if it has already closed again. */
    if (f->power == FS_POWER_DROP && !module->power_losses && module->powered &&
        guest_ns >= f->power_off_at_ns) {
        module->powered = false;
        module->power_losses++;
        power_reset(module, POWER_OFF_LATCH, emit);
    }
    bool up = rail_up(f, guest_ns);
    if (up == module->powered)
        return;
    module->powered = up;
    if (!up)
        module->power_losses++;
    power_reset(module, up ? POWER_ON_LATCH : POWER_OFF_LATCH, emit);
}

/* Boot state: a module configured without a rail starts without one, and not as a loss event. */
void fs_power_init(FsModule *module) {
    if (rail_up(&module->faults, 0))
        return;
    module->powered = false;
    module->latch = POWER_OFF_LATCH;
}

bool fs_fault_absent(FsModule *module, int64_t guest_ns) {
    FsFaults *f = &module->faults;
    fs_power_sync(module, guest_ns, true);
    bool absent = !module->powered || f->i2c == FS_I2C_MISSING ||
                  (f->i2c == FS_I2C_DISAPPEAR && guest_ns >= f->disappear_at_ns) ||
                  (f->i2c == FS_I2C_NACK && chance(f, f->nack));
    if (!absent && f->power == FS_POWER_BROWNOUT && chance(f, f->brownout)) {
        /* The rail dips below reset and recovers within the transaction: it is lost, and the
         * expander comes back in its power-on state. */
        module->power_losses++;
        power_reset(module, POWER_ON_LATCH, true);
        absent = true;
    }
    if (absent)
        f->events++;
    return absent;
}

/* A controller supply sag resets the I2C peripheral mid-transaction: the address was
 * acknowledged, but the bytes after it are lost and reads float high. */
bool fs_fault_sag(FsModule *module) {
    FsFaults *f = &module->faults;
    if (f->power != FS_POWER_SAG || !chance(f, f->sag))
        return false;
    f->events++;
    if (!module->observed_power) {
        module->observed_power = true;
        fs.fault_event_pending = true;
    }
    return true;
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
    if (f->power)
        json_object_object_add(object, "power", json_object_new_string(POWER[f->power]));
    json_object_object_add(object, "events", json_object_new_int64((int64_t)f->events));
    return object;
}

struct json_object *fs_fault_summary(void) {
    struct json_object *array = NULL;
    for (int i = 0; i < fs.count; i++) {
        if (!fs.modules[i].faults.configured)
            continue;
        if (!array)
            array = json_object_new_array();
        struct json_object *entry = fs_faults_json(&fs.modules[i]);
        json_object_object_add(entry, "bus", json_object_new_int(fs.modules[i].bus));
        json_object_object_add(entry, "addr", json_object_new_int(fs.modules[i].addr));
        json_object_array_add(array, entry);
    }
    return array;
}

bool fs_validate_fault_summary(struct json_object *record, bool legacy) {
    struct json_object *actual = NULL;
    bool present = json_object_object_get_ex(record, "faults", &actual);
    struct json_object *expected = fs_fault_summary();
    bool valid = present == (expected != NULL);
    if (valid && expected && legacy) {
        valid = json_object_is_type(actual, json_type_array) &&
                json_object_array_length(actual) == json_object_array_length(expected);
        for (size_t i = 0; valid && i < json_object_array_length(expected); i++) {
            struct json_object *entry = json_object_array_get_idx(actual, i), *events;
            valid = json_object_object_get_ex(entry, "events", &events) &&
                    json_object_is_type(events, json_type_int) &&
                    json_object_get_int64(events) >= 0;
            if (valid)
                json_object_object_add(json_object_array_get_idx(expected, i), "events",
                                       json_object_get(events));
        }
    }
    if (valid && expected)
        valid = json_object_equal(expected, actual);
    if (expected)
        json_object_put(expected);
    return valid;
}
