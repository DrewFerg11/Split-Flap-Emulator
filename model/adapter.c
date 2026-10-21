/* SPDX-License-Identifier: GPL-2.0-only */
/* Compile against the exact configured QEMU headers; core model stays independent. */
#include "../qemu/callbacks.h"
#include "control.h"
#include "model.h"
#include "qemu/osdep.h"

#include "qemu/main-loop.h"
#include "qemu/timer.h"
#include "sysemu/reset.h"
#include <dlfcn.h>
#include <errno.h>
#include <limits.h>
#include <sys/socket.h>

static __typeof__(qemu_clock_get_ns) *clock_ns;
static __typeof__(timer_init_full) *initialize_timer;
static __typeof__(timer_mod) *modify_timer;
static __typeof__(timer_del) *delete_timer;
static __typeof__(qemu_register_reset) *register_reset;
static __typeof__(qemu_unregister_reset) *unregister_reset;
static QEMUTimer snapshot_timer;
static __typeof__(qemu_set_fd_handler) *set_fd_handler;
static bool (*owns_bql)(void);
static int control_fd = -1;

static void check_model(void) {
    if (*fs_model_error()) {
        fprintf(stderr, "flapsim model failed: %s\n", fs_model_error());
        exit(1);
    }
}

static void *required_symbol(void *library, const char *name) {
    void *value = dlsym(library, name);
    if (!value) {
        fprintf(stderr, "flapsim model: missing QEMU symbol %s: %s\n", name, dlerror());
        exit(1);
    }
    return value;
}

static void guest_reset(void *opaque) {
    (void)opaque;
    fs_model_tick(clock_ns(QEMU_CLOCK_VIRTUAL));
    fs_model_guest_reset();
    check_model();
}

static void snapshot(void *opaque) {
    (void)opaque;
    int64_t now = clock_ns(QEMU_CLOCK_VIRTUAL);
    fs_model_tick(now);
    check_model();
    modify_timer(&snapshot_timer, now + 100000000LL);
}

static void control_read(void *opaque) {
    (void)opaque;
    /* The pinned main-loop I/O dispatcher holds the BQL even while paused. */
    if (!owns_bql() || !fs_control_serve(control_fd, clock_ns(QEMU_CLOCK_VIRTUAL))) {
        set_fd_handler(control_fd, NULL, NULL, NULL);
        close(control_fd);
        control_fd = -1;
    }
}

void flapsim_model_prepare(void *library) {
    clock_ns = required_symbol(library, "qemu_clock_get_ns");
    initialize_timer = required_symbol(library, "timer_init_full");
    modify_timer = required_symbol(library, "timer_mod");
    delete_timer = required_symbol(library, "timer_del");
    register_reset = required_symbol(library, "qemu_register_reset");
    unregister_reset = required_symbol(library, "qemu_unregister_reset");
    if (fs_model_init(getenv("FLAPSIM_CONFIG")) != 0)
        check_model();
    const char *descriptor = getenv("FLAPSIM_CONTROL_FD");
    if (descriptor) {
        char *end;
        errno = 0;
        long value = strtol(descriptor, &end, 10);
        int type;
        socklen_t size = sizeof(type);
        if (errno || !*descriptor || *end || value < 0 || value > INT_MAX ||
            getsockopt((int)value, SOL_SOCKET, SO_TYPE, &type, &size) || type != SOCK_SEQPACKET) {
            fprintf(stderr, "flapsim control: invalid inherited descriptor\n");
            exit(1);
        }
        control_fd = (int)value;
        set_fd_handler = required_symbol(library, "qemu_set_fd_handler");
        owns_bql = required_symbol(library, "bql_locked");
    }
}

void flapsim_model_start(void) {
    /* Reset registration creates QOM objects and requires initialized QEMU subsystems. */
    register_reset(guest_reset, NULL);
    initialize_timer(&snapshot_timer, NULL, QEMU_CLOCK_VIRTUAL, 1, 0, snapshot, NULL);
    modify_timer(&snapshot_timer, clock_ns(QEMU_CLOCK_VIRTUAL) + 100000000LL);
    if (control_fd >= 0)
        set_fd_handler(control_fd, control_read, NULL, NULL);
}

void flapsim_model_finish(void) {
    if (control_fd >= 0) {
        set_fd_handler(control_fd, NULL, NULL, NULL);
        close(control_fd);
        control_fd = -1;
    }
    delete_timer(&snapshot_timer);
    unregister_reset(guest_reset, NULL);
    fs_model_close();
    check_model();
}

int flapsim_i2c_event(uint8_t bus, uint8_t addr, uint16_t event) {
    int result = fs_model_event(bus, addr, event, clock_ns(QEMU_CLOCK_VIRTUAL));
    check_model();
    return result;
}
