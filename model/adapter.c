/* SPDX-License-Identifier: GPL-2.0-only */
/* Compile against the exact configured QEMU headers; core model stays independent. */
#include "../qemu/callbacks.h"
#include "model.h"
#include "qemu/osdep.h"
#include "qemu/timer.h"
#include "sysemu/reset.h"
#include <dlfcn.h>

static __typeof__(qemu_clock_get_ns) *clock_ns;
static __typeof__(timer_init_full) *initialize_timer;
static __typeof__(timer_mod) *modify_timer;
static __typeof__(timer_del) *delete_timer;
static __typeof__(qemu_register_reset) *register_reset;
static __typeof__(qemu_unregister_reset) *unregister_reset;
static QEMUTimer snapshot_timer;

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

void flapsim_model_prepare(void *library) {
    clock_ns = required_symbol(library, "qemu_clock_get_ns");
    initialize_timer = required_symbol(library, "timer_init_full");
    modify_timer = required_symbol(library, "timer_mod");
    delete_timer = required_symbol(library, "timer_del");
    register_reset = required_symbol(library, "qemu_register_reset");
    unregister_reset = required_symbol(library, "qemu_unregister_reset");
    if (fs_model_init(getenv("FLAPSIM_CONFIG")) != 0)
        check_model();
}

void flapsim_model_start(void) {
    /* Reset registration creates QOM objects and requires initialized QEMU subsystems. */
    register_reset(guest_reset, NULL);
    initialize_timer(&snapshot_timer, NULL, QEMU_CLOCK_VIRTUAL, 1, 0, snapshot, NULL);
    modify_timer(&snapshot_timer, clock_ns(QEMU_CLOCK_VIRTUAL) + 100000000LL);
}

void flapsim_model_finish(void) {
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
