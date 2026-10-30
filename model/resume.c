/* SPDX-License-Identifier: GPL-2.0-only */
#define _POSIX_C_SOURCE 200809L
#include "internal.h"
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>

static bool integer(struct json_object *object, const char *key, int64_t lower, int64_t upper,
                    int64_t *out) {
    struct json_object *value;
    if (!json_object_object_get_ex(object, key, &value) ||
        !json_object_is_type(value, json_type_int))
        return false;
    *out = json_object_get_int64(value);
    return *out >= lower && *out <= upper;
}

static bool counters(struct json_object *object, FsCounters *out) {
    struct json_object *value;
    int64_t parts[6];
    const char *keys[6] = {"forward", "backward", "stalls", "writes", "reads", "anomalies"};
    if (!json_object_object_get_ex(object, "counters", &value) ||
        !json_object_is_type(value, json_type_object))
        return false;
    for (int i = 0; i < 6; i++) {
        if (!integer(value, keys[i], 0, INT64_MAX, &parts[i]))
            return false;
    }
    /* Written only when nonzero, and each one is also a stall. */
    int64_t lost = 0;
    if (json_object_object_get_ex(value, "lost_write_stalls", NULL) &&
        !integer(value, "lost_write_stalls", 1, parts[2], &lost))
        return false;
    *out =
        (FsCounters){(uint64_t)parts[0], (uint64_t)parts[1], (uint64_t)parts[2], (uint64_t)parts[3],
                     (uint64_t)parts[4], (uint64_t)parts[5], (uint64_t)lost};
    return true;
}

static bool module(struct json_object *entry, FsModule *target) {
    int64_t bus, addr, angle, latch, phase;
    struct json_object *energized;
    FsCounters count;
    if (!json_object_is_type(entry, json_type_object) || !integer(entry, "bus", 0, 1, &bus) ||
        !integer(entry, "addr", 0, 127, &addr) || bus != target->bus || addr != target->addr ||
        !integer(entry, "angle", 0, target->steps - 1, &angle) ||
        !integer(entry, "latch", 0, 65535, &latch) || !integer(entry, "phase", -1, 3, &phase) ||
        !json_object_object_get_ex(entry, "energized", &energized) ||
        !json_object_is_type(energized, json_type_boolean) || !counters(entry, &count))
        return false;
    target->angle = (int)angle;
    target->latch = (uint16_t)latch;
    target->phase = (int)phase;
    target->energized = json_object_get_boolean(energized);
    target->counters = count;
    if (fs.faults_enabled) {
        struct json_object *hall;
        if (!json_object_object_get_ex(entry, "hall", &hall) ||
            !json_object_is_type(hall, json_type_boolean) ||
            json_object_get_boolean(hall) != fs_hall(target))
            return false;
    }
    return true;
}

/* The final record is complete and validated against this configuration; a torn final line
 * (from a reset during a write) is discarded so appended records stay one JSON per line. */
static bool restore(const char *text, size_t end, bool *recorded_reset) {
    size_t start = end;
    while (start > 0 && text[start - 1] != '\n')
        start--;
    char *line = strndup(text + start, end - start);
    struct json_object *record = line ? json_tokener_parse(line) : NULL;
    free(line);
    int64_t version, guest, sequence, movement, settlement;
    struct json_object *modules;
    FsCounters total;
    bool valid = record && json_object_is_type(record, json_type_object) &&
                 integer(record, "schema_version", 1, 1, &version) &&
                 integer(record, "guest_ns", 0, INT64_MAX, &guest) &&
                 integer(record, "sequence", 0, INT64_MAX, &sequence) &&
                 integer(record, "movement_id", 0, INT64_MAX, &movement) &&
                 integer(record, "settlement_id", 0, INT64_MAX, &settlement) &&
                 counters(record, &total) &&
                 json_object_object_get_ex(record, "modules", &modules) &&
                 json_object_is_type(modules, json_type_array) &&
                 (int)json_object_array_length(modules) == fs.count;
    if (valid)
        valid = fs_restore_faults(record) && fs_restore_overlay(record);
    uint64_t anomalies = 0;
    for (int i = 0; valid && i < fs.count; i++) {
        valid = module(json_object_array_get_idx(modules, i), &fs.modules[i]);
        anomalies += fs.modules[i].counters.anomalies;
    }
    if (valid && anomalies != total.anomalies)
        valid = false;
    struct json_object *event;
    if (valid && (!json_object_object_get_ex(record, "event", &event) ||
                  !json_object_is_type(event, json_type_string)))
        valid = false;
    if (valid) {
        *recorded_reset = !strcmp(json_object_get_string(event), "guest_reset");
        /* New records persist reset generations even before the first mechanics edit.
         * Legacy timelines have no count; retain the one reset their last event proves. */
        struct json_object *resets;
        int64_t generation;
        if (json_object_object_get_ex(record, "guest_resets", &resets)) {
            if (!integer(record, "guest_resets", 0, INT64_MAX, &generation)) {
                json_object_put(record);
                return false;
            }
            fs.guest_resets = (uint64_t)generation;
        } else {
            fs.guest_resets = *recorded_reset ? 1 : 0;
        }
        fs.now = guest;
        fs.sequence = (uint64_t)sequence;
        fs.movement_id = (uint64_t)movement;
        fs.settlement_id = (uint64_t)settlement;
        fs.totals.anomalies = anomalies;
    }
    if (record)
        json_object_put(record);
    return valid;
}

bool fs_restore(const char *timeline_path, bool *recorded_reset) {
    FILE *file = fopen(timeline_path, "r+");
    if (!file) {
        fs_fail("cannot resume: missing timeline %s", timeline_path);
        return false;
    }
    char *text = NULL;
    size_t size = 0, capacity = 0;
    char chunk[65536];
    for (size_t count; (count = fread(chunk, 1, sizeof(chunk), file)) > 0; size += count) {
        if (size + count + 1 > capacity) {
            capacity = (size + count + 1) * 2;
            char *grown = realloc(text, capacity);
            if (!grown) {
                free(text);
                fclose(file);
                fs_fail("out of memory resuming timeline");
                return false;
            }
            text = grown;
        }
        memcpy(text + size, chunk, count);
    }
    size_t end = size;
    while (end > 0 && text[end - 1] != '\n')
        end--;
    bool ok = end > 0 && !ferror(file) && restore(text, end - 1, recorded_reset) &&
              fflush(file) == 0 && ftruncate(fileno(file), (off_t)end) == 0;
    free(text);
    if (fclose(file) != 0)
        ok = false;
    if (!ok)
        fs_fail("cannot resume from the last timeline record in %s", timeline_path);
    return ok;
}
