/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef FLAPSIM_CONTROL_H
#define FLAPSIM_CONTROL_H
#include <stdint.h>

/* One bounded, nonblocking request on a private SOCK_SEQPACKET connection.
 * Run only in the model owner context. Returns 0 on disconnect/transport error.
 * Mutations apply only between transactions while all coils are off.
 * Native callers must hold host::model_lock(). */
int fs_control_serve(int fd, int64_t guest_ns);
#endif
