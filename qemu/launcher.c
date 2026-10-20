/* SPDX-License-Identifier: GPL-2.0-only */
#define _POSIX_C_SOURCE 200809L
#include "callbacks.h"
#include <dlfcn.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static void pin_event(int pin, int value) {
    (void)pin;
    (void)value;
}
static uint8_t spi_event(uint8_t id, uint16_t event) {
    (void)id;
    (void)event;
    return 0;
}
static void uart_event(uint8_t id, uint8_t value) {
    (void)id;
    (void)value;
}
static void rmt_event(uint8_t channel, uint32_t config0, uint32_t value) {
    (void)channel;
    (void)config0;
    (void)value;
}

static void *symbol(void *library, const char *name) {
    void *result = dlsym(library, name);
    if (!result) {
        fprintf(stderr, "flapsim-qemu: missing symbol %s: %s\n", name, dlerror());
        exit(1);
    }
    return result;
}

int main(int argc, char **argv) {
    char path[PATH_MAX];
    ssize_t length = readlink("/proc/self/exe", path, sizeof(path) - 1);
    if (length < 0 || (size_t)length >= sizeof(path) - 1) {
        fprintf(stderr, "flapsim-qemu: cannot resolve executable path\n");
        return 1;
    }
    path[length] = '\0';
    char *slash = strrchr(path, '/');
    if (!slash || (size_t)(slash - path) + sizeof("/../lib/libqemu-xtensa.so") > sizeof(path)) {
        return 1;
    }
    strcpy(slash, "/../lib/libqemu-xtensa.so");
    void *library = dlopen(path, RTLD_NOW | RTLD_GLOBAL);
    if (!library) {
        fprintf(stderr, "flapsim-qemu: %s\n", dlerror());
        return 1;
    }
    void (*register_callbacks)(void *) = symbol(library, "qemu_picsimlab_register_callbacks");
    void (*initialize)(int, char **) = symbol(library, "qemu_init");
    int (*main_loop)(void) = symbol(library, "qemu_main_loop");
    void (*cleanup)(int) = symbol(library, "qemu_cleanup");
    flapsim_callbacks callbacks = {pin_event,  pin_event, flapsim_i2c_event, spi_event,
                                   uart_event, NULL,      rmt_event};
    register_callbacks(&callbacks);
    /* One board per process. All lifecycle calls stay on this original thread. */
    initialize(argc, argv);
    int status = main_loop();
    cleanup(status);
    /* QEMU owns process-global/atexit state: leave the library loaded until exit. */
    return status;
}
