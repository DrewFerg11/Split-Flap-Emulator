/* SPDX-License-Identifier: GPL-2.0-only */
#define _POSIX_C_SOURCE 200809L
#include "control.h"
#include "internal.h"
#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

int fs_model_power_checkpoint(int64_t guest_ns) {
    if (!fs.initialized || fs.failed || guest_ns < fs.now)
        return -1;
    fs_model_tick(guest_ns);
    fs_emit("controller_power_off", false);
    if (!fs.failed && fsync(fileno(fs.timeline)) != 0)
        fs_fail("cannot commit power checkpoint");
    return fs.failed ? -1 : 0;
}

static int serve(int fd, int64_t guest_ns, bool native) {
    char request[2049];
    ssize_t length = recv(fd, request, sizeof(request) - 1, MSG_DONTWAIT | MSG_TRUNC);
    if (length < 0)
        return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR;
    if (length == 0)
        return 0;
    static const char expected[] = "FLAPSIM_STATE/1";
    static const char reset[] = "FLAPSIM_RESET/1";
    static const char power[] = "FLAPSIM_POWER_OFF/1";
    if (native && fs.initialized && !fs.failed && length == sizeof(power) - 1 &&
        !memcmp(request, power, sizeof(power) - 1))
        return 3; /* The native owner checkpoints and replies while retaining its lock. */
    if (native && fs.initialized && !fs.failed && length == sizeof(reset) - 1 &&
        !memcmp(request, reset, sizeof(reset) - 1)) {
        const char accepted[] = "{\"status\":\"accepted\"}";
        ssize_t sent = send(fd, accepted, sizeof(accepted) - 1, MSG_DONTWAIT | MSG_NOSIGNAL);
        return sent == sizeof(accepted) - 1 ? 2 : 0;
    }
    char *state = NULL;
    if (length == sizeof(expected) - 1 && !memcmp(request, expected, sizeof(expected) - 1))
        state = fs_model_snapshot(guest_ns);
    else if (length > 0 && length < (ssize_t)sizeof(request) &&
             !memchr(request, 0, (size_t)length)) {
        request[length] = 0;
        state = fs_control_command(request, guest_ns);
    }
    const char *reply = state ? state : "{\"error\":\"snapshot_unavailable_or_invalid_request\"}";
    size_t size = strlen(reply);
    ssize_t sent = send(fd, reply, size, MSG_DONTWAIT | MSG_NOSIGNAL);
    free(state);
    /* A slow/disconnected consumer disables control, never blocks the firmware. */
    return sent >= 0 && (size_t)sent == size;
}

int fs_control_serve(int fd, int64_t guest_ns) { return serve(fd, guest_ns, false); }
int fs_control_serve_native(int fd, int64_t guest_ns) { return serve(fd, guest_ns, true); }
