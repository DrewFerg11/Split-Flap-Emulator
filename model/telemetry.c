/* SPDX-License-Identifier: GPL-2.0-only */
/* Optional best-effort transport. It never waits for a reader or changes model physics. */
#define _POSIX_C_SOURCE 200809L
#include "internal.h"
#include <errno.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <time.h>

#define MAGIC 0x464c415049324331ULL
#define EVENTS 8
/* Wire words are host-endian uint64_t: both ends run on the same host. */
enum { TRANSACTION, SOURCE_START, RESET, POWER_OFF, SOURCE_END, STATE };
enum { FRAGMENT, STOP, REPLACED, RESET_END, POWER_END, SOURCE_END_REASON, ORPHAN };
typedef struct {
    uint64_t id, address, generation, count, events[EVENTS][5];
} Frame;
static struct {
    int fd;
    bool active;
    uint64_t source, sequence, raw_sequence, next_transaction, drops, dropped_events;
    int64_t last_state;
    Frame buses[2];
} telemetry;

static int64_t host_ns(void) {
    struct timespec now;
    return clock_gettime(CLOCK_MONOTONIC, &now) == 0
               ? (int64_t)now.tv_sec * 1000000000LL + now.tv_nsec
               : 0;
}

static void emit(uint64_t kind, uint64_t bus, uint64_t address, uint64_t transaction,
                 uint64_t generation, uint64_t end, const void *payload, size_t size,
                 uint64_t count, int64_t guest_ns) {
    if (!telemetry.active || telemetry.sequence == UINT64_MAX)
        return;
    uint64_t words[14] = {MAGIC,
                          telemetry.source,
                          ++telemetry.sequence,
                          transaction,
                          generation,
                          bus,
                          address,
                          kind,
                          end,
                          count,
                          (uint64_t)guest_ns,
                          telemetry.drops,
                          telemetry.dropped_events,
                          telemetry.raw_sequence};
    struct iovec vectors[2] = {{words, sizeof(words)}, {(void *)payload, size}};
    struct msghdr message = {.msg_iov = vectors, .msg_iovlen = size ? 2 : 1};
    ssize_t sent = sendmsg(telemetry.fd, &message, MSG_DONTWAIT | MSG_NOSIGNAL);
    if (sent != (ssize_t)(sizeof(words) + size)) {
        telemetry.drops++;
        if (kind == TRANSACTION)
            telemetry.dropped_events += count;
    }
}

static void flush(int bus, uint64_t end, int64_t guest_ns) {
    Frame *frame = &telemetry.buses[bus];
    if (!frame->id && !frame->count)
        return;
    emit(TRANSACTION, (uint64_t)bus, frame->address, frame->id, frame->generation, end,
         frame->events, (size_t)frame->count * sizeof(frame->events[0]), frame->count, guest_ns);
    frame->count = 0;
    if (end != FRAGMENT)
        frame->id = 0;
}

void fs_telemetry_start(void) {
    memset(&telemetry, 0, sizeof(telemetry));
    telemetry.fd = -1;
    const char *value = getenv("FLAPSIM_I2C_FD");
    if (!value)
        return;
    char *end;
    errno = 0;
    long fd = strtol(value, &end, 10);
    int type;
    socklen_t length = sizeof(type);
    if (errno || !*value || *end || fd < 3 || fd > INT_MAX ||
        getsockopt((int)fd, SOL_SOCKET, SO_TYPE, &type, &length) || type != SOCK_SEQPACKET ||
        getrandom(&telemetry.source, sizeof(telemetry.source), GRND_NONBLOCK) !=
            sizeof(telemetry.source)) {
        fs_fail("cannot initialize structured I2C telemetry");
        return;
    }
    telemetry.fd = (int)fd;
    telemetry.active = true;
    emit(SOURCE_START, UINT64_MAX, 0, 0, fs.guest_resets, 0, NULL, 0, 0, fs.now);
}

void fs_telemetry_event(uint8_t bus, uint8_t address, uint16_t event, int result,
                        int64_t guest_ns) {
    if (!telemetry.active)
        return;
    uint64_t item[5] = {++telemetry.raw_sequence, (uint64_t)guest_ns, address, event,
                        (uint64_t)result};
    if (bus > 1) {
        emit(TRANSACTION, bus, address, 0, fs.guest_resets, ORPHAN, item, sizeof(item), 1,
             guest_ns);
        return;
    }
    Frame *frame = &telemetry.buses[bus];
    int kind = event & 255;
    if (kind <= 2) {
        flush(bus, REPLACED, guest_ns);
        frame->id = ++telemetry.next_transaction;
        frame->address = address;
        frame->generation = fs.guest_resets;
    } else if (!frame->id) {
        frame->address = address;
        frame->generation = fs.guest_resets;
    }
    memcpy(frame->events[frame->count++], item, sizeof(item));
    if (kind == 3)
        flush(bus, STOP, guest_ns);
    else if (frame->count == EVENTS)
        flush(bus, FRAGMENT, guest_ns);
}

void fs_telemetry_boundary(int kind) {
    if (!telemetry.active)
        return;
    for (int bus = 0; bus < 2; bus++)
        flush(bus,
              kind == RESET       ? RESET_END
              : kind == POWER_OFF ? POWER_END
                                  : SOURCE_END_REASON,
              fs.now);
    emit((uint64_t)kind, UINT64_MAX, 0, 0, fs.guest_resets, 0, NULL, 0, 0, fs.now);
}

void fs_telemetry_state(int64_t guest_ns) {
    if (!telemetry.active)
        return;
    int64_t now = host_ns();
    if (now - telemetry.last_state < 200000000LL)
        return;
    telemetry.last_state = now;
    char *state = fs_model_snapshot(guest_ns);
    if (state) {
        size_t length = strlen(state);
        if (length <= 65536)
            emit(STATE, UINT64_MAX, 0, 0, fs.guest_resets, 0, state, length, length, guest_ns);
        free(state);
    }
}

void fs_telemetry_close(void) {
    fs_telemetry_boundary(SOURCE_END);
    /* The runner owns the descriptor; retain it across native re-exec. */
    telemetry.active = false;
}
