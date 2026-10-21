/* SPDX-License-Identifier: GPL-2.0-only */
#include "vclock.h"

#include <atomic>
#include <cmath>
#include <ctime>
#include <stdexcept>
#include <thread>

namespace flapsim::vclock {
namespace {

/* Seqlock: firmware busy-waits read the clock constantly; rate changes are rare. */
std::atomic<uint64_t> sequence{0};
std::atomic<int64_t> anchor_host{0}, anchor_guest{0};
std::atomic<double> rate{1.0};
std::atomic<int64_t> boot_anchor{0};
std::mutex writer;

int64_t host_now() {
    timespec value;
    clock_gettime(CLOCK_MONOTONIC, &value);
    return int64_t(value.tv_sec) * 1000000000 + value.tv_nsec;
}

int64_t guest_at(int64_t host) {
    while (true) {
        uint64_t before = sequence.load(std::memory_order_acquire);
        if (before & 1)
            continue;
        int64_t base_host = anchor_host.load(std::memory_order_relaxed);
        int64_t base_guest = anchor_guest.load(std::memory_order_relaxed);
        double current = rate.load(std::memory_order_relaxed);
        std::atomic_thread_fence(std::memory_order_acquire);
        if (sequence.load(std::memory_order_relaxed) == before)
            return base_guest + int64_t(std::llround(double(host - base_host) * current));
    }
}

void check_speed(double value) {
    if (!std::isfinite(value) || value < kMinSpeed || value > kMaxSpeed)
        throw std::invalid_argument("virtual clock speed must be in [0.25, 50]");
}

} // namespace

void init(int64_t guest_base_ns, int64_t boot_guest_ns, double value) {
    check_speed(value);
    std::lock_guard<std::mutex> lock(writer);
    sequence.fetch_add(1, std::memory_order_acq_rel);
    anchor_host.store(host_now(), std::memory_order_relaxed);
    anchor_guest.store(guest_base_ns, std::memory_order_relaxed);
    rate.store(value, std::memory_order_relaxed);
    boot_anchor.store(boot_guest_ns, std::memory_order_relaxed);
    sequence.fetch_add(1, std::memory_order_release);
}

int64_t now_ns() { return guest_at(host_now()); }

int64_t boot_guest_ns() { return boot_anchor.load(std::memory_order_relaxed); }

int64_t boot_ns() { return now_ns() - boot_guest_ns(); }

double speed() { return rate.load(std::memory_order_relaxed); }

void set_speed(double value) {
    check_speed(value);
    std::lock_guard<std::mutex> lock(writer);
    int64_t host = host_now();
    int64_t guest = guest_at(host);
    sequence.fetch_add(1, std::memory_order_acq_rel);
    anchor_host.store(host, std::memory_order_relaxed);
    anchor_guest.store(guest, std::memory_order_relaxed);
    rate.store(value, std::memory_order_relaxed);
    sequence.fetch_add(1, std::memory_order_release);
}

std::chrono::nanoseconds host_duration(int64_t guest_duration) {
    if (guest_duration <= 0)
        return std::chrono::nanoseconds(0);
    return std::chrono::nanoseconds(int64_t(std::ceil(double(guest_duration) / speed())));
}

void sleep_ns(int64_t duration) {
    if (duration <= 0) {
        std::this_thread::yield();
        return;
    }
    int64_t target = now_ns() + duration;
    /* Re-check after waking: the rate may have changed while asleep. */
    for (int64_t remaining = duration; remaining > 0; remaining = target - now_ns())
        std::this_thread::sleep_for(host_duration(remaining));
}

} // namespace flapsim::vclock
