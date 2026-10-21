/* SPDX-License-Identifier: GPL-2.0-only */
#include "control.h"
#include "model.h"
#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>

int fs_control_serve(int fd, int64_t guest_ns) {
    char request[64];
    ssize_t length = recv(fd, request, sizeof(request), MSG_DONTWAIT | MSG_TRUNC);
    if (length < 0)
        return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR;
    if (length == 0)
        return 0;
    static const char expected[] = "FLAPSIM_STATE/1";
    char *state = NULL;
    if (length == sizeof(expected) - 1 && !memcmp(request, expected, sizeof(expected) - 1))
        state = fs_model_snapshot(guest_ns);
    const char *reply = state ? state : "{\"error\":\"snapshot_unavailable_or_invalid_request\"}";
    size_t size = strlen(reply);
    ssize_t sent = send(fd, reply, size, MSG_DONTWAIT | MSG_NOSIGNAL);
    free(state);
    /* A slow/disconnected consumer disables control, never blocks the firmware. */
    return sent >= 0 && (size_t)sent == size;
}
