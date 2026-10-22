/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <mutex>

/* One virtual clock for firmware time, FreeRTOS waits and the hardware model timeline.
 * Guest time advances at `speed` times host monotonic time and continues across emulated
 * reboots; boot time restarts at zero on every boot, like esp_timer on the device. */
namespace flapsim::vclock {

constexpr double kMinSpeed = 0.25;
constexpr double kMaxSpeed = 50.0;

void init(int64_t guest_base_ns, int64_t boot_guest_ns, double speed);
int64_t now_ns();
int64_t boot_ns();
int64_t boot_guest_ns();
double speed();
/* Re-anchors at the current instant so guest time never jumps when the rate changes. */
void set_speed(double speed);
void sleep_ns(int64_t duration);
std::chrono::nanoseconds host_duration(int64_t guest_duration);

/* External wait primitives re-evaluate guest deadlines at least every 5 ms of host time.
 * Clock-owned sleeps are notified immediately on a rate change. */
std::chrono::nanoseconds wait_slice(int64_t guest_remaining);

/* Wait on a condition variable for at most `guest_timeout` virtual nanoseconds. */
template <typename Predicate>
bool wait_for(std::condition_variable &condition, std::unique_lock<std::mutex> &lock,
              int64_t guest_timeout, Predicate ready) {
    int64_t target = now_ns() + std::max<int64_t>(0, guest_timeout);
    while (!ready()) {
        int64_t remaining = target - now_ns();
        if (remaining <= 0)
            return ready();
        condition.wait_for(lock, wait_slice(remaining));
    }
    return true;
}

} // namespace flapsim::vclock
