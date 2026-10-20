/* SPDX-License-Identifier: GPL-2.0-only */
/* Test double for the pinned loader ABI, never linked into production. */
#include "callbacks.h"
#include <assert.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static pthread_t owner;
static int stage;

void qemu_picsimlab_register_callbacks(void *arg) {
    flapsim_callbacks *callbacks = arg;
    assert(stage++ == 0);
    owner = pthread_self();
    assert(callbacks->write_pin && callbacks->dir_pin && callbacks->i2c_event);
    assert(callbacks->spi_event && callbacks->uart_tx_event && callbacks->rmt_event);
    assert(callbacks->pinmap == NULL);
    assert(callbacks->i2c_event(0, 0x20, 1) == 0);
    assert(callbacks->i2c_event(0, 0x21, 1) != 0);
    assert(callbacks->i2c_event(1, 0x20, 1) != 0);
    puts("register");
}

void qemu_init(int argc, char **argv) {
    if (argc == 2 && (!strcmp(argv[1], "--version") || !strcmp(argv[1], "--help"))) {
        assert(stage == 0);
        puts("QEMU fixture information");
        exit(0);
    }
    assert(stage++ == 1 && pthread_equal(owner, pthread_self()));
    assert(argc == 2 && strcmp(argv[1], "--fixture") == 0);
    puts("initialize");
}

int qemu_main_loop(void) {
    assert(stage++ == 2 && pthread_equal(owner, pthread_self()));
    puts("main_loop");
    return 23;
}

#ifndef OMIT_CLEANUP
void qemu_cleanup(int status) {
    assert(stage++ == 3 && pthread_equal(owner, pthread_self()));
    assert(status == 23);
    puts("cleanup");
}
#endif
