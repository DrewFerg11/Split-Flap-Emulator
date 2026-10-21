/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include "esp_system.h"

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <string>

/* Process-level services behind the shims: run configuration, the serial port, the shared
 * hardware model and the emulated reset path (re-exec of this binary). */
namespace flapsim::host {

struct WiFiConfig {
    bool station_connects = true;
    int64_t connect_delay_ms = 1500;
    std::string ip = "127.0.0.1";
};

struct Config {
    std::string model_config, serial_log, serial_input, nvs, littlefs;
    double speed = 1.0;
    /* Listening socket for the device's port 80, inherited from the runner; -1 for none. */
    int http_fd = -1;
    uint32_t random_seed = 1;
    WiFiConfig wifi;
};

const Config &config();
unsigned boot_count();
esp_reset_reason_t reset_reason();

void serial_write(const void *data, size_t size);
int serial_available();
int serial_read();
int serial_peek();

/* The C model is single-threaded; every call into it holds this lock. */
std::timed_mutex &model_lock();
/* Called with model_lock() held after any model call; exits like the QEMU adapter on failure. */
void check_model();

/* Parses `--config <file>`, then boots: clock, serial, panic handlers, banner and model. */
void start(int argc, char **argv);
/* Unit tests: adopt a configuration and open its serial log, without a model or banner. */
void configure_for_tests(const Config &config);

[[noreturn]] void restart(esp_reset_reason_t reason);
/* A fatal firmware error: prints an ESP32-style abort record, then reboots like the device. */
[[noreturn]] void panic(const char *what);

} // namespace flapsim::host
