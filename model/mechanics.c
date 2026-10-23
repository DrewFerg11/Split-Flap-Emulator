/* SPDX-License-Identifier: GPL-2.0-only */
#define _POSIX_C_SOURCE 200809L
#include "internal.h"
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define FS_MAX_ID 9007199254740991LL

static void number(struct json_object *object, const char *key, int64_t value) {
    json_object_object_add(object, key, json_object_new_int64(value));
}

static bool integer(struct json_object *object, const char *key, int64_t lo, int64_t hi,
                    int64_t *out) {
    struct json_object *value;
    if (!json_object_object_get_ex(object, key, &value) ||
        !json_object_is_type(value, json_type_int))
        return false;
    *out = json_object_get_int64(value);
    return *out >= lo && *out <= hi;
}

struct json_object *fs_initial_faults(void) {
    struct json_object *array = json_object_new_array();
    for (int i = 0; i < fs.count; i++)
        json_object_array_add(array, fs_faults(&fs.initial_modules[i]));
    return array;
}

bool fs_restore_faults(struct json_object *record) {
    struct json_object *version, *initial, *modules, *entry, *value, *effects;
    bool enabled = json_object_object_get_ex(record, "faults_version", &version);
    if (!json_object_object_get_ex(record, "modules", &modules) ||
        !json_object_is_type(modules, json_type_array) ||
        json_object_array_length(modules) != (size_t)fs.count)
        return false;
    if (!enabled) {
        if (json_object_object_get_ex(record, "initial_faults", &initial) ||
            !fs_validate_fault_summary(record, true))
            return false;
        for (int i = 0; i < fs.count; i++) {
            entry = json_object_array_get_idx(modules, i);
            if (json_object_object_get_ex(entry, "faults", &value) ||
                json_object_object_get_ex(entry, "fault_counters", &value))
                return false;
        }
        /* Older #19 records have only a summary: retain their documented reseed
         * behavior. Newly emitted records include full versioned resume state. */
        return true;
    }
    if (!json_object_is_type(version, json_type_int) || json_object_get_int64(version) != 1 ||
        !json_object_object_get_ex(record, "initial_faults", &initial))
        return false;
    struct json_object *configured = fs_initial_faults();
    bool valid = json_object_equal(configured, initial);
    json_object_put(configured);
    for (int i = 0; valid && i < fs.count; i++) {
        entry = json_object_array_get_idx(modules, i);
        int64_t blocked, nacks, rng, events;
        valid = json_object_object_get_ex(entry, "faults", &value) &&
                fs_load_fault_state(value, &fs.modules[i]) &&
                json_object_object_get_ex(entry, "fault_counters", &effects) &&
                json_object_is_type(effects, json_type_object) &&
                json_object_object_length(effects) == 4 &&
                integer(effects, "blocked_steps", 0, INT64_MAX, &blocked) &&
                integer(effects, "nacks", 0, INT64_MAX, &nacks) &&
                integer(effects, "rng", 0, UINT32_MAX, &rng) &&
                integer(effects, "events", 0, INT64_MAX, &events);
        if (valid) {
            fs.modules[i].blocked_steps = (uint64_t)blocked;
            fs.modules[i].nacks = (uint64_t)nacks;
            fs.modules[i].faults.rng = (uint32_t)rng;
            fs.modules[i].faults.events = (uint64_t)events;
            if ((rng == 0) != (fs.modules[i].faults.seed == 0) ||
                (uint64_t)events < (uint64_t)blocked + (uint64_t)nacks)
                valid = false;
        }
    }
    valid = valid && fs_validate_fault_summary(record, false);
    if (valid)
        fs.faults_enabled = true;
    return valid;
}

struct json_object *fs_mechanics(const FsModule *module) {
    struct json_object *object = json_object_new_object();
    number(object, "steps_per_rotation", module->steps);
    number(object, "magnet_position", module->magnet);
    number(object, "flap_offset", module->flap_offset);
    number(object, "sensor_window", module->window);
    json_object_object_add(
        object, "sensor_polarity",
        json_object_new_string(module->active_low ? "active-low" : "active-high"));
    return object;
}

struct json_object *fs_initial_mechanics(void) {
    struct json_object *array = json_object_new_array();
    for (int i = 0; i < fs.count; i++)
        json_object_array_add(array, fs_mechanics(&fs.initial_modules[i]));
    return array;
}

/* Work on a copy: inherited values must fit the complete resulting geometry. */
bool fs_parse_mechanics(struct json_object *object, FsModule *module, bool partial) {
    if (!object || !json_object_is_type(object, json_type_object))
        return false;
    int count = json_object_object_length(object);
    if (count < 1 || (!partial && count != 5))
        return false;
    json_object_object_foreach(object, key, value) {
        if (!strcmp(key, "faults")) {
            if (!partial || !fs_patch_faults(value, module))
                return false;
        } else if (!strcmp(key, "sensor_polarity")) {
            if (!json_object_is_type(value, json_type_string))
                return false;
            const char *text = json_object_get_string(value);
            if (strlen(text) != (size_t)json_object_get_string_len(value))
                return false;
            if (!strcmp(text, "active-high"))
                module->active_low = false;
            else if (!strcmp(text, "active-low"))
                module->active_low = true;
            else
                return false;
        } else {
            if (!json_object_is_type(value, json_type_int))
                return false;
            int64_t n = json_object_get_int64(value);
            if (n < 0 || n > 1000000)
                return false;
            if (!strcmp(key, "steps_per_rotation"))
                module->steps = (int)n;
            else if (!strcmp(key, "magnet_position"))
                module->magnet = (int)n;
            else if (!strcmp(key, "flap_offset"))
                module->flap_offset = (int)n;
            else if (!strcmp(key, "sensor_window"))
                module->window = (int)n;
            else
                return false;
        }
    }
    return module->steps >= 4 && module->steps >= fs.nflaps && module->angle < module->steps &&
           module->magnet < module->steps && module->flap_offset < module->steps &&
           module->window >= 1 && module->window <= module->steps;
}

/* Versioned additive extension: legacy v1 evidence remains unchanged until the first edit. */
bool fs_restore_overlay(struct json_object *record) {
    struct json_object *value, *initial, *modules, *operation, *command, *patch, *flaps;
    if (!json_object_object_get_ex(record, "mechanics_version", &value))
        return !json_object_object_get_ex(record, "mechanics_revision", &value) &&
               !json_object_object_get_ex(record, "last_operation", &value) &&
               !json_object_object_get_ex(record, "initial_mechanics", &value) &&
               !json_object_object_get_ex(record, "initial_flaps", &value);
    int64_t version, revision, resets, id, bus, addr, expected, sequence, guest;
    if (!integer(record, "mechanics_version", 1, 1, &version) ||
        !integer(record, "mechanics_revision", 1, FS_MAX_ID, &revision) ||
        !json_object_object_get_ex(record, "initial_flaps", &flaps) ||
        !json_object_is_type(flaps, json_type_string) ||
        json_object_get_string_len(flaps) != fs.nflaps ||
        memcmp(json_object_get_string(flaps), fs.flaps, (size_t)fs.nflaps) ||
        !integer(record, "guest_resets", 0, INT64_MAX, &resets) ||
        !json_object_object_get_ex(record, "initial_mechanics", &initial) ||
        !json_object_object_get_ex(record, "modules", &modules) ||
        !json_object_is_type(modules, json_type_array) ||
        (int)json_object_array_length(modules) != fs.count ||
        !json_object_object_get_ex(record, "last_operation", &operation) ||
        !json_object_is_type(operation, json_type_object) ||
        json_object_object_length(operation) != 4 ||
        !integer(operation, "revision", revision, revision, &version) ||
        !integer(operation, "sequence", 1, INT64_MAX, &sequence) ||
        !integer(operation, "guest_ns", 0, INT64_MAX, &guest) ||
        !json_object_object_get_ex(operation, "command", &command) ||
        !json_object_is_type(command, json_type_object) ||
        json_object_object_length(command) != 5 || !integer(command, "id", 1, FS_MAX_ID, &id) ||
        !integer(command, "bus", 0, 1, &bus) || !integer(command, "addr", 32, 39, &addr) ||
        !integer(command, "expected_revision", revision - 1, revision - 1, &expected) ||
        !json_object_object_get_ex(command, "patch", &patch))
        return false;
    struct json_object *configured = fs_initial_mechanics();
    bool valid = json_object_equal(configured, initial);
    json_object_put(configured);
    int index = fs_find((int)bus, (int)addr);
    if (index < 0)
        return false;
    for (int i = 0; valid && i < fs.count; i++) {
        struct json_object *entry = json_object_array_get_idx(modules, i), *physical;
        /* Restore geometry before the dynamic angle, which can exceed the initial rotation. */
        fs.modules[i].angle = 0;
        valid = json_object_is_type(entry, json_type_object) &&
                json_object_object_get_ex(entry, "mechanics", &physical) &&
                fs_parse_mechanics(physical, &fs.modules[i], false);
    }
    /* fs_restore_faults ran first: this is the recorded EFFECTIVE state, not
     * initial hardware. Reapplying the last patch must reproduce that state. */
    FsModule copy = fs.modules[index];
    struct json_object *physical = fs_mechanics(&copy);
    struct json_object *original_faults = fs_faults(&copy), *fault_patch;
    if (json_object_object_get_ex(patch, "faults", &fault_patch) &&
        !json_object_object_get_ex(record, "faults_version", &value))
        valid = false;
    valid = valid && fs_parse_mechanics(patch, &copy, true);
    struct json_object *patched = fs_mechanics(&copy);
    valid = valid && json_object_equal(physical, patched);
    json_object_put(physical);
    json_object_put(patched);
    struct json_object *patched_faults = fs_faults(&copy);
    valid = valid && json_object_equal(original_faults, patched_faults);
    json_object_put(original_faults);
    json_object_put(patched_faults);
    int64_t record_sequence, record_guest;
    valid = valid && integer(record, "sequence", sequence, INT64_MAX, &record_sequence) &&
            integer(record, "guest_ns", guest, INT64_MAX, &record_guest);
    if (valid) {
        fs.mechanics_revision = (uint64_t)revision;
        fs.guest_resets = (uint64_t)resets;
        fs.operation = json_object_get(operation);
    }
    return valid;
}

static char *rejected(const char *error) {
    struct json_object *object = json_object_new_object();
    json_object_object_add(object, "error", json_object_new_string(error));
    const char *text = json_object_to_json_string_ext(object, JSON_C_TO_STRING_PLAIN);
    char *reply = malloc(strlen(text) + 1);
    if (reply)
        strcpy(reply, text);
    json_object_put(object);
    return reply;
}

/* Private protocol is emitted only by the typed runner dispatcher. No asynchronous queue. */
char *fs_control_command(const char *request, int64_t guest_ns) {
    const char prefix[] = "FLAPSIM_MECH/1 ";
    if (strncmp(request, prefix, sizeof(prefix) - 1))
        return rejected("invalid_request");
    if (!fs.initialized || fs.failed || guest_ns < fs.now)
        return rejected("backend_unavailable");
    const char *text = request + sizeof(prefix) - 1;
    struct json_tokener *tokener = json_tokener_new();
    json_tokener_set_flags(tokener, JSON_TOKENER_STRICT);
    struct json_object *command = json_tokener_parse_ex(tokener, text, (int)strlen(text));
    bool parsed = json_tokener_get_error(tokener) == json_tokener_success &&
                  json_tokener_get_parse_end(tokener) == strlen(text);
    json_tokener_free(tokener);
    int64_t id, bus, addr, revision;
    struct json_object *patch;
    const char *error = "invalid_request";
    if (!parsed || !command || !json_object_is_type(command, json_type_object) ||
        json_object_object_length(command) != 5 || !integer(command, "id", 1, FS_MAX_ID, &id) ||
        !integer(command, "bus", 0, 1, &bus) || !integer(command, "addr", 32, 39, &addr) ||
        !integer(command, "expected_revision", 0, FS_MAX_ID - 1, &revision) ||
        !json_object_object_get_ex(command, "patch", &patch))
        goto done;
    if (fs.operation) {
        struct json_object *previous;
        int64_t previous_id = 0;
        json_object_object_get_ex(fs.operation, "command", &previous);
        integer(previous, "id", 1, FS_MAX_ID, &previous_id);
        if (id <= previous_id) {
            if (id == previous_id && json_object_equal(previous, command)) {
                json_object_put(command);
                return fs_model_snapshot(guest_ns);
            }
            error = "stale_or_reused_id";
            goto done;
        }
    }
    int index = fs_find((int)bus, (int)addr);
    if (index < 0) {
        error = "module_not_found";
        goto done;
    }
    FsModule changed = fs.modules[index];
    if (!fs_parse_mechanics(patch, &changed, true)) {
        error = "invalid_mechanics";
        goto done;
    }
    if ((uint64_t)revision != fs.mechanics_revision) {
        error = "revision_conflict";
        goto done;
    }
    /* No changes mid-transaction, or while any motor remains energized. Busy is retryable. */
    struct json_object *fault_patch;
    bool changes_faults = json_object_object_get_ex(patch, "faults", &fault_patch);
    bool fault_only = changes_faults && json_object_object_length(patch) == 1;
    if ((!fault_only && fs_any_energized()) || fs.buses[0].active || fs.buses[1].active) {
        error = "model_busy";
        goto done;
    }
    if (fs.sequence >= INT64_MAX || fs.mechanics_revision >= FS_MAX_ID) {
        error = "revision_exhausted";
        goto done;
    }
    struct json_object *operation = json_object_new_object();
    json_object_object_add(operation, "command", json_object_get(command));
    number(operation, "revision", fs.mechanics_revision + 1);
    number(operation, "sequence", fs.sequence + 1);
    number(operation, "guest_ns", guest_ns);
    if (fs.operation)
        json_object_put(fs.operation);
    fs.operation = operation;
    if (changes_faults)
        changed.observed_nack = changed.observed_block = false;
    fs.modules[index] = changed;
    if (changes_faults)
        fs.faults_enabled = true;
    fs.mechanics_revision++;
    fs.now = guest_ns;
    fs_emit(changes_faults ? "faults_changed" : "mechanics_changed", !fs_any_energized());
    if (!fs.failed && fsync(fileno(fs.timeline)) != 0)
        fs_fail("cannot commit mechanics timeline");
    json_object_put(command);
    /* Never acknowledge success when recording the result failed. */
    return fs_model_snapshot(guest_ns);
done:
    if (command)
        json_object_put(command);
    return rejected(error);
}
