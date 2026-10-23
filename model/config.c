/* SPDX-License-Identifier: GPL-2.0-only */
#define _POSIX_C_SOURCE 200809L
#include "internal.h"
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>

void fs_fail(const char *format, ...) {
    if (fs.failed)
        return;
    fs.failed = true;
    va_list args;
    va_start(args, format);
    vsnprintf(fs.error, sizeof(fs.error), format, args);
    va_end(args);
}

const char *fs_model_error(void) { return fs.error; }

void fs_model_close(void) {
    if (fs.operation)
        json_object_put(fs.operation);
    fs.operation = NULL;
    if (fs.timeline)
        fclose(fs.timeline);
    if (fs.trace)
        fclose(fs.trace);
    free(fs.state_path);
    free(fs.activity_path);
    fs.timeline = fs.trace = NULL;
    fs.state_path = fs.activity_path = NULL;
    fs.initialized = false;
}

static struct json_object *field(struct json_object *obj, const char *key, enum json_type type) {
    struct json_object *value;
    if (!obj || !json_object_is_type(obj, json_type_object) ||
        !json_object_object_get_ex(obj, key, &value) || !json_object_is_type(value, type)) {
        fs_fail("normalized model config: missing or invalid %s", key);
        return NULL;
    }
    return value;
}

static int number(struct json_object *obj, const char *key, int lower, int upper) {
    struct json_object *value = field(obj, key, json_type_int);
    int64_t integer = value ? json_object_get_int64(value) : -1;
    if (integer < lower || integer > upper) {
        fs_fail("normalized model config: %s must be in [%d, %d]", key, lower, upper);
        return lower;
    }
    return (int)integer;
}

static int optional_number(struct json_object *obj, const char *key, int lower, int upper,
                           int fallback) {
    struct json_object *value;
    if (obj && json_object_is_type(obj, json_type_object) &&
        !json_object_object_get_ex(obj, key, &value))
        return fallback;
    return number(obj, key, lower, upper);
}

static bool polarity(struct json_object *obj, bool fallback) {
    struct json_object *value;
    if (obj && json_object_is_type(obj, json_type_object) &&
        !json_object_object_get_ex(obj, "sensor_polarity", &value))
        return fallback;
    value = field(obj, "sensor_polarity", json_type_string);
    if (value) {
        const char *text = json_object_get_string(value);
        if (strlen(text) == (size_t)json_object_get_string_len(value)) {
            if (!strcmp(text, "active-high"))
                return false;
            if (!strcmp(text, "active-low"))
                return true;
        }
    }
    fs_fail("normalized sensor_polarity must be active-high or active-low");
    return fallback;
}

static char *output_path(struct json_object *outputs, const char *key) {
    struct json_object *value = field(outputs, key, json_type_string);
    if (!value)
        return NULL;
    const char *path = json_object_get_string(value);
    if (path[0] != '/' || strlen(path) != (size_t)json_object_get_string_len(value)) {
        fs_fail("model output %s must be an absolute path without NUL", key);
        return NULL;
    }
    return strdup(path);
}

/* Parse and validate the normalized config; returns the timeline path for the caller to open. */
static char *load(const char *config_path, const char *mode) {
    fs_model_close();
    memset(&fs, 0, sizeof(fs));
    for (int b = 0; b < 2; b++)
        fs.buses[b].selected = fs.buses[b].hidden = -1;
    struct json_object *config = config_path ? json_object_from_file(config_path) : NULL;
    if (!config || !json_object_is_type(config, json_type_object)) {
        fs_fail("cannot read normalized model JSON: %s", config_path ? config_path : "(unset)");
        if (config)
            json_object_put(config);
        return NULL;
    }
    number(config, "schema_version", 1, 1);
    struct json_object *chip = field(config, "chip", json_type_string);
    if (chip && strcmp(json_object_get_string(chip), "esp32"))
        fs_fail("chip must be esp32");
    struct json_object *drum = field(config, "drum", json_type_object);
    fs.steps = number(drum, "steps_per_rotation", 4, 1000000);
    fs.magnet = number(drum, "magnet_position", 0, fs.steps - 1);
    fs.window = number(drum, "sensor_window", 1, fs.steps);
    int flap_offset = optional_number(drum, "flap_offset", 0, fs.steps - 1, 0);
    bool active_low = polarity(drum, false);
    struct json_object *flaps = field(drum, "flaps", json_type_string);
    if (flaps) {
        fs.nflaps = json_object_get_string_len(flaps);
        const char *text = json_object_get_string(flaps);
        if (fs.nflaps < 2 || fs.nflaps > 95 || fs.nflaps > fs.steps)
            fs_fail("invalid flap count");
        else {
            for (int i = 0; i < fs.nflaps; i++) {
                if (text[i] < 32 || text[i] > 126 || memchr(text, text[i], i))
                    fs_fail("invalid flap characters");
            }
            memcpy(fs.flaps, text, fs.nflaps);
        }
    }
    struct json_object *modules = field(config, "modules", json_type_array);
    size_t count = modules ? json_object_array_length(modules) : 0;
    if (count < 1 || count > FS_MAX_MODULES)
        fs_fail("model requires 1–16 modules");
    else {
        for (size_t i = 0; i < count; i++) {
            struct json_object *entry = json_object_array_get_idx(modules, i);
            FsModule *module = &fs.modules[i];
            module->bus = number(entry, "bus", 0, 1);
            module->addr = number(entry, "addr", 0x20, 0x27);
            module->steps = optional_number(entry, "steps_per_rotation",
                                            fs.nflaps > 4 ? fs.nflaps : 4, 1000000, fs.steps);
            module->magnet =
                optional_number(entry, "magnet_position", 0, module->steps - 1, fs.magnet);
            module->flap_offset =
                optional_number(entry, "flap_offset", 0, module->steps - 1, flap_offset);
            module->window = optional_number(entry, "sensor_window", 1, module->steps, fs.window);
            module->active_low = polarity(entry, active_low);
            if (module->flap_offset >= module->steps)
                fs_fail("flap_offset exceeds module steps_per_rotation");
            if (module->magnet >= module->steps)
                fs_fail("magnet_position exceeds module steps_per_rotation");
            if (module->window > module->steps)
                fs_fail("sensor_window exceeds module steps_per_rotation");
            module->angle = number(entry, "angle", 0, module->steps - 1);
            fs_parse_faults(entry, module);
            if (module->faults.configured)
                fs.faults_enabled = true;
            module->phase = -1;
            module->latch = 0xffe1;
            if (i &&
                (module->bus < fs.modules[i - 1].bus || (module->bus == fs.modules[i - 1].bus &&
                                                         module->addr <= fs.modules[i - 1].addr))) {
                fs_fail("normalized modules must be unique and sorted by bus/address");
            }
        }
        fs.count = (int)count;
        memcpy(fs.initial_modules, fs.modules, sizeof(fs.modules));
    }
    struct json_object *outputs = field(config, "outputs", json_type_object);
    fs.state_path = output_path(outputs, "state");
    fs.activity_path = output_path(outputs, "activity");
    char *timeline = output_path(outputs, "timeline");
    struct json_object *trace;
    if (outputs && json_object_object_get_ex(outputs, "trace", &trace) && trace) {
        char *path = output_path(outputs, "trace");
        if (!fs.failed) {
            fs.trace = fopen(path, mode);
            if (!fs.trace)
                fs_fail("cannot open I2C trace: %s", path);
        }
        free(path);
    }
    json_object_put(config);
    if (fs.failed) {
        free(timeline);
        fs_model_close();
        return NULL;
    }
    return timeline;
}

static int start(char *timeline, const char *mode, const char *event, bool state) {
    /* event is NULL when the restored record already describes this reset. */
    if (!timeline) {
        fs_model_close();
        return -1;
    }
    if (!fs.failed) {
        fs.timeline = fopen(timeline, mode);
        if (!fs.timeline)
            fs_fail("cannot open timeline: %s", timeline);
    }
    free(timeline);
    if (fs.failed) {
        fs_model_close();
        return -1;
    }
    fs.initialized = true;
    if (event)
        fs_emit(event, state);
    else
        fs_write_activity();
    if (fs.failed) {
        fs_model_close();
        return -1;
    }
    return 0;
}

int fs_model_init(const char *config_path) {
    return start(load(config_path, "w"), "w", "initialized", true);
}

int fs_model_resume(const char *config_path, int64_t guest_ns) {
    char *timeline = load(config_path, "a");
    bool recorded_reset = false;
    if (timeline && !fs_restore(timeline, &recorded_reset)) {
        free(timeline);
        timeline = NULL;
    }
    if (timeline && guest_ns < fs.now) {
        fs_fail("cannot resume: guest clock moved backwards");
        free(timeline);
        timeline = NULL;
    }
    /* Like a QEMU guest reset: the controller restarts, the expanders and drums do not. A clean
     * native reset already recorded its exact guest_reset; after a crash the last record may lag
     * the drums by up to one snapshot interval, and the reset is recorded here instead. */
    fs.now = fs.last_snapshot = guest_ns;
    if (timeline && !recorded_reset)
        fs.guest_resets++;
    return start(timeline, "a", recorded_reset ? NULL : "guest_reset", false);
}
