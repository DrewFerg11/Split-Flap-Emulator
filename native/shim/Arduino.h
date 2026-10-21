/* SPDX-License-Identifier: GPL-2.0-only */
/* arduino-esp32 2.x core API for the native host build. */
#pragma once

#include <cinttypes>
#include <cmath>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>

#include "esp32-hal-log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "pgmspace.h"

#define HIGH 0x1
#define LOW 0x0
#define INPUT 0x01
#define OUTPUT 0x03
#define INPUT_PULLUP 0x05
#define PI 3.1415926535897932384626433832795
#define PROGMEM
#define constrain(amt, low, high) ((amt) < (low) ? (low) : ((amt) > (high) ? (high) : (amt)))
#define sq(x) ((x) * (x))
#define radians(deg) ((deg) * (PI / 180.0))
#define degrees(rad) ((rad) * (180.0 / PI))
#define bitRead(value, bit) (((value) >> (bit)) & 0x01)
#define bitSet(value, bit) ((value) |= (1UL << (bit)))
#define bitClear(value, bit) ((value) &= ~(1UL << (bit)))
#define lowByte(w) ((uint8_t)((w) & 0xff))
#define highByte(w) ((uint8_t)((w) >> 8))

typedef bool boolean;
typedef uint8_t byte;
typedef unsigned int word;

unsigned long millis();
unsigned long micros();
void delay(uint32_t milliseconds);
void delayMicroseconds(uint32_t microseconds);
void yield();
void pinMode(uint8_t pin, uint8_t mode);
void digitalWrite(uint8_t pin, uint8_t value);
int digitalRead(uint8_t pin);
long map(long value, long from_low, long from_high, long to_low, long to_high);

void setup(void);
void loop(void);

#ifdef __cplusplus
#include <algorithm>
#include <cmath>

#include "Client.h"
#include "Esp.h"
#include "HardwareSerial.h"
#include "IPAddress.h"
#include "Print.h"
#include "Printable.h"
#include "Stream.h"
#include "WString.h"

using std::abs;
using std::isinf;
using std::isnan;
using std::max;
using std::min;
using std::round;

/* Deterministic per run: seeded from the native config and the boot number, not hardware RNG. */
long random(long upper);
long random(long lower, long upper);
void randomSeed(unsigned long seed);

extern "C" bool getLocalTime(struct tm *info, uint32_t milliseconds = 5000);
extern "C" void configTime(long gmt_offset, int daylight_offset, const char *server1,
                           const char *server2 = nullptr, const char *server3 = nullptr);
extern "C" void configTzTime(const char *tz, const char *server1, const char *server2 = nullptr,
                             const char *server3 = nullptr);
#endif
