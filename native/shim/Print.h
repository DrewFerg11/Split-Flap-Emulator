/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include "Printable.h"
#include "WString.h"

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <ctime>

#define DEC 10
#define HEX 16
#define OCT 8
#define BIN 2

class Print {
  public:
    virtual ~Print() = default;
    virtual size_t write(uint8_t value) = 0;
    virtual size_t write(const uint8_t *buffer, size_t size);
    size_t write(const char *text) {
        return text ? write(reinterpret_cast<const uint8_t *>(text), std::strlen(text)) : 0;
    }
    size_t write(const char *buffer, size_t size) {
        return write(reinterpret_cast<const uint8_t *>(buffer), size);
    }
    virtual void flush() {}

    size_t printf(const char *format, ...) __attribute__((format(printf, 2, 3)));

    size_t print(const __FlashStringHelper *text) {
        return print(reinterpret_cast<const char *>(text));
    }
    size_t print(const String &text) { return write(text.c_str(), text.length()); }
    size_t print(const char *text) { return write(text); }
    size_t print(char value) { return write(uint8_t(value)); }
    size_t print(unsigned char value, int base = DEC) { return print((unsigned long)value, base); }
    size_t print(int value, int base = DEC) { return print((long)value, base); }
    size_t print(unsigned int value, int base = DEC) { return print((unsigned long)value, base); }
    size_t print(long value, int base = DEC);
    size_t print(unsigned long value, int base = DEC);
    size_t print(long long value, int base = DEC);
    size_t print(unsigned long long value, int base = DEC);
    size_t print(double value, int digits = 2);
    size_t print(struct tm *time, const char *format = nullptr);
    size_t print(const Printable &value) { return value.printTo(*this); }

    size_t println() { return print("\r\n"); }
    template <typename T> size_t println(const T &value) { return print(value) + println(); }
    template <typename T> size_t println(const T &value, int option) {
        return print(value, option) + println();
    }
};
