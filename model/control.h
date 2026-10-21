/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef FLAPSIM_CONTROL_H
#define FLAPSIM_CONTROL_H
#include <stdint.h>

/* One bounded, nonblocking request on a private SOCK_SEQPACKET connection.
 * Run only in the model owner context. Returns 0 on disconnect/transport error.
 * No model mutations; safe for native callers holding host::model_lock(). */
int fs_control_serve(int fd, int64_t guest_ns);
#endif
