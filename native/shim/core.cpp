/* SPDX-License-Identifier: GPL-2.0-only */
#include "Arduino.h"
#include "WiFi.h"
#include "host.h"
#include "vclock.h"

#include <atomic>
#include <cstdlib>
#include <mutex>
#include <thread>

HardwareSerial Serial;
EspClass ESP;

unsigned long millis() { return (unsigned long)(flapsim::vclock::boot_ns() / 1000000); }

unsigned long micros() { return (unsigned long)(flapsim::vclock::boot_ns() / 1000); }

void delay(uint32_t milliseconds) { flapsim::vclock::sleep_ns(int64_t(milliseconds) * 1000000); }

void delayMicroseconds(uint32_t microseconds) {
    /* The device busy-waits here, so the caller keeps its core; so does the host thread. */
    int64_t target = flapsim::vclock::now_ns() + int64_t(microseconds) * 1000;
    while (flapsim::vclock::now_ns() < target)
        std::this_thread::yield();
}

void yield() { std::this_thread::yield(); }

void pinMode(uint8_t, uint8_t) {}
void digitalWrite(uint8_t, uint8_t) {}
int digitalRead(uint8_t) { return LOW; }

long map(long value, long from_low, long from_high, long to_low, long to_high) {
    long divisor = from_high - from_low;
    if (divisor == 0)
        return -1;
    return (value - from_low) * (to_high - to_low) / divisor + to_low;
}

namespace {
std::mutex random_mutex;
uint32_t random_state = 0;

uint32_t next_random() {
    std::lock_guard<std::mutex> lock(random_mutex);
    if (random_state == 0)
        random_state =
            (flapsim::host::config().random_seed ^ (flapsim::host::boot_count() * 0x9e3779b9u))
                ?: 0x6d2b79f5u;
    random_state ^= random_state << 13;
    random_state ^= random_state >> 17;
    random_state ^= random_state << 5;
    return random_state;
}
} // namespace

uint32_t esp_random(void) { return next_random(); }

long random(long upper) { return upper <= 0 ? 0 : long(next_random() % uint32_t(upper)); }

long random(long lower, long upper) {
    if (lower >= upper)
        return lower;
    return random(upper - lower) + lower;
}

void randomSeed(unsigned long seed) {
    if (seed == 0)
        return;
    std::lock_guard<std::mutex> lock(random_mutex);
    random_state = uint32_t(seed) ?: 1;
}

int64_t esp_timer_get_time(void) { return flapsim::vclock::boot_ns() / 1000; }

esp_reset_reason_t esp_reset_reason(void) { return flapsim::host::reset_reason(); }

void esp_restart(void) { flapsim::host::restart(ESP_RST_SW); }

void EspClass::restart() { esp_restart(); }

void HardwareSerial::begin(unsigned long rate, uint32_t, int8_t, int8_t, bool, unsigned long,
                           uint8_t) {
    baud = rate;
}

int HardwareSerial::available() { return flapsim::host::serial_available(); }
int HardwareSerial::read() { return flapsim::host::serial_read(); }
int HardwareSerial::peek() { return flapsim::host::serial_peek(); }

size_t HardwareSerial::write(const uint8_t *buffer, size_t size) {
    flapsim::host::serial_write(buffer, size);
    return size;
}

namespace {
std::atomic<bool> sntp_started{false};
} // namespace

extern "C" void configTzTime(const char *tz, const char *, const char *, const char *) {
    setenv("TZ", tz, 1);
    tzset();
    sntp_started = true;
}

/* The firmware's view of wall time; host code keeps using the real clock functions. */
extern "C" time_t time(time_t *out) noexcept {
    using namespace flapsim;
    const auto &clock = host::config().clock;
    auto &set = host::rtc_clock_set();
    if (!set && (clock.sync_at_boot || (sntp_started && WiFi.status() == WL_CONNECTED)))
        set = true;
    time_t value;
    if (!set) {
        value = time_t(vclock::now_ns() / 1000000000); /* RTC counting since power-on */
    } else if (clock.fixed) {
        value = time_t(clock.start + vclock::now_ns() / 1000000000);
    } else {
        timespec real;
        clock_gettime(CLOCK_REALTIME, &real);
        value = real.tv_sec;
    }
    if (out)
        *out = value;
    return value;
}

extern "C" void configTime(long gmt_offset, int daylight_offset, const char *server1,
                           const char *server2, const char *server3) {
    char tz[32];
    long offset = -(gmt_offset + daylight_offset);
    std::snprintf(tz, sizeof(tz), "UTC%+ld:%02ld", offset / 3600, std::labs(offset) % 3600 / 60);
    configTzTime(tz, server1, server2, server3);
}

extern "C" bool getLocalTime(struct tm *info, uint32_t milliseconds) {
    unsigned long start = millis();
    do {
        time_t now = time(nullptr);
        localtime_r(&now, info);
        if (info->tm_year > 2016 - 1900)
            return true;
        delay(10);
    } while (millis() - start <= milliseconds);
    return false;
}
