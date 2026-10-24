/* SPDX-License-Identifier: GPL-2.0-only */
#include "internal.h"
#include <errno.h>
#include <stdlib.h>
#include <string.h>

static void integer(struct json_object *object, const char *key, int64_t value) {
    json_object_object_add(object, key, json_object_new_int64(value));
}

static struct json_object *counters(FsCounters count) {
    struct json_object *object = json_object_new_object();
    integer(object, "forward", count.forward);
    integer(object, "backward", count.backward);
    integer(object, "stalls", count.stalls);
    integer(object, "writes", count.writes);
    integer(object, "reads", count.reads);
    integer(object, "anomalies", count.anomalies);
    return object;
}

static struct json_object *identity(int64_t guest_ns) {
    struct json_object *object = json_object_new_object();
    integer(object, "schema_version", 1);
    integer(object, "guest_resets", fs.guest_resets);
    if (fs.faults_enabled) {
        integer(object, "faults_version", 1);
        json_object_object_add(object, "initial_faults", fs_initial_faults());
    }
    if (fs.mechanics_revision) {
        integer(object, "mechanics_version", 1);
        json_object_object_add(object, "initial_flaps", json_object_new_string(fs.flaps));
        integer(object, "mechanics_revision", fs.mechanics_revision);
        json_object_object_add(object, "initial_mechanics", fs_initial_mechanics());
        json_object_object_add(object, "last_operation", json_object_get(fs.operation));
    }
    integer(object, "guest_ns", guest_ns);
    integer(object, "sequence", fs.sequence);
    integer(object, "movement_id", fs.movement_id);
    integer(object, "settlement_id", fs.settlement_id);
    json_object_object_add(object, "settled", json_object_new_boolean(!fs_any_energized()));
    FsCounters total = {0};
    for (int i = 0; i < fs.count; i++) {
        FsCounters *count = &fs.modules[i].counters;
        total.forward += count->forward;
        total.backward += count->backward;
        total.stalls += count->stalls;
        total.writes += count->writes;
        total.reads += count->reads;
    }
    total.anomalies = fs.totals.anomalies;
    json_object_object_add(object, "counters", counters(total));
    struct json_object *faults = fs_fault_summary();
    if (faults)
        json_object_object_add(object, "faults", faults);
    return object;
}

static void atomic_json(const char *path, struct json_object *object) {
    if (fs.failed)
        return;
    size_t length = strlen(path) + 5;
    char *temporary = malloc(length);
    if (!temporary) {
        fs_fail("out of memory writing JSON");
        return;
    }
    snprintf(temporary, length, "%s.tmp", path);
    FILE *file = fopen(temporary, "w");
    if (!file)
        fs_fail("cannot open %s: %s", temporary, strerror(errno));
    else {
        bool failed = fprintf(file, "%s\n",
                              json_object_to_json_string_ext(object, JSON_C_TO_STRING_PLAIN)) < 0;
        if (fclose(file) != 0)
            failed = true;
        if (failed || rename(temporary, path) != 0)
            fs_fail("cannot commit JSON output %s", path);
    }
    free(temporary);
}

void fs_write_activity(void) {
    if (fs.failed)
        return;
    struct json_object *object = identity(fs.now);
    atomic_json(fs.activity_path, object);
    json_object_put(object);
}

static struct json_object *snapshot(int64_t guest_ns, bool mechanics) {
    struct json_object *object = identity(guest_ns);
    struct json_object *modules = json_object_new_array();
    char text[FS_MAX_MODULES + 1] = {0}, rows[2][FS_MAX_MODULES + 1] = {{0}};
    int row_count[2] = {0};
    for (int i = 0; i < fs.count; i++) {
        FsModule *module = &fs.modules[i];
        struct json_object *entry = json_object_new_object();
        integer(entry, "bus", module->bus);
        integer(entry, "addr", module->addr);
        integer(entry, "angle", module->angle);
        double position = fs_flap_position(module);
        json_object_object_add(entry, "flap_position", json_object_new_double(position));
        int nearest = (int)(position + 0.5);
        json_object_object_add(entry, "flap_error", json_object_new_double(position - nearest));
        integer(entry, "latch", module->latch);
        integer(entry, "phase", module->phase);
        char shown = fs_shown(module);
        text[i] = shown;
        rows[module->bus][row_count[module->bus]++] = shown;
        json_object_object_add(entry, "char", json_object_new_string_len(&shown, 1));
        json_object_object_add(entry, "hall", json_object_new_boolean(fs_hall(module)));
        json_object_object_add(entry, "energized", json_object_new_boolean(module->energized));
        json_object_object_add(entry, "counters", counters(module->counters));
        if (fs.faults_enabled) {
            json_object_object_add(entry, "faults", fs_faults(module));
            struct json_object *effects = json_object_new_object();
            integer(effects, "blocked_steps", module->blocked_steps);
            integer(effects, "nacks", module->nacks);
            integer(effects, "rng", module->faults.rng);
            integer(effects, "events", module->faults.events);
            if (fs_power_evidence(module)) {
                integer(effects, "power_losses", module->power_losses);
                json_object_object_add(effects, "powered",
                                       json_object_new_boolean(module->powered));
            }
            json_object_object_add(entry, "fault_counters", effects);
        }
        if (mechanics || fs.mechanics_revision) {
            json_object_object_add(entry, "mechanics", fs_mechanics(module));
            integer(entry, "coils", (module->latch >> 1) & 15);
        }
        json_object_array_add(modules, entry);
    }
    json_object_object_add(object, "modules", modules);
    json_object_object_add(object, "text", json_object_new_string(text));
    struct json_object *row_array = json_object_new_array();
    for (int b = 0; b < 2; b++)
        json_object_array_add(row_array, json_object_new_string(rows[b]));
    json_object_object_add(object, "rows", row_array);
    return object;
}

char *fs_model_snapshot(int64_t guest_ns) {
    if (!fs.initialized || fs.failed || guest_ns < fs.now)
        return NULL;
    struct json_object *object = snapshot(guest_ns, true);
    const char *text = json_object_to_json_string_ext(object, JSON_C_TO_STRING_PLAIN);
    size_t length = strlen(text) + 1;
    char *copy = malloc(length);
    if (copy)
        memcpy(copy, text, length);
    json_object_put(object);
    return copy;
}

void fs_emit(const char *event, bool state) {
    if (fs.failed)
        return;
    fs.sequence++;
    struct json_object *object = snapshot(fs.now, false);
    json_object_object_add(object, "event", json_object_new_string(event));
    if (state)
        atomic_json(fs.state_path, object);
    if (fprintf(fs.timeline, "%s\n",
                json_object_to_json_string_ext(object, JSON_C_TO_STRING_PLAIN)) < 0 ||
        fflush(fs.timeline) != 0)
        fs_fail("cannot write model timeline");
    fs_write_activity();
    json_object_put(object);
}
