/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once

/* arduino-esp32 2.x error-level logging: "[%6u][E][file:line] function(): message\r\n".
 * Shims that stand in for core code pass the core's own file and line, so serial evidence
 * reads the same as the QEMU mode's real firmware. */
void flapsim_log(char level, const char *file, unsigned line, const char *function,
                 const char *format, ...) __attribute__((format(printf, 5, 6)));

#define log_e(format, ...) flapsim_log('E', __FILE__, __LINE__, __FUNCTION__, format, ##__VA_ARGS__)
/* The release builds' default level is error: lower levels are type-checked but not printed. */
#define FLAPSIM_LOG_DISABLED(format, ...)                                                          \
    do {                                                                                           \
        if (0)                                                                                     \
            flapsim_log('V', __FILE__, __LINE__, __FUNCTION__, format, ##__VA_ARGS__);             \
    } while (0)
#define log_w(format, ...) FLAPSIM_LOG_DISABLED(format, ##__VA_ARGS__)
#define log_i(format, ...) FLAPSIM_LOG_DISABLED(format, ##__VA_ARGS__)
#define log_d(format, ...) FLAPSIM_LOG_DISABLED(format, ##__VA_ARGS__)
#define log_v(format, ...) FLAPSIM_LOG_DISABLED(format, ##__VA_ARGS__)
