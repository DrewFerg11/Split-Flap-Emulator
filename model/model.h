/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef FLAPSIM_MODEL_H
#define FLAPSIM_MODEL_H
#include <stdint.h>

int fs_model_init(const char *config_path);
/* Native-mode reboot: reload the config, restore the last complete timeline record (normally the
 * guest_reset that fs_model_guest_reset recorded before the restart) and append to it. */
int fs_model_resume(const char *config_path, int64_t guest_ns);
void fs_model_close(void);
const char *fs_model_error(void);
int fs_model_event(uint8_t bus, uint8_t address, uint16_t event, int64_t guest_ns);
void fs_model_tick(int64_t guest_ns);
void fs_model_guest_reset(void);

#endif
