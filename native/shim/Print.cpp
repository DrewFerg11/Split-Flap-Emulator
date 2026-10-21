/* SPDX-License-Identifier: GPL-2.0-only */
#include "Print.h"

#include <cmath>
#include <cstdarg>
#include <cstdio>
#include <vector>

size_t Print::write(const uint8_t *buffer, size_t size) {
    size_t written = 0;
    while (size--) {
        if (!write(*buffer++))
            break;
        written++;
    }
    return written;
}

size_t Print::printf(const char *format, ...) {
    va_list args, copy;
    va_start(args, format);
    va_copy(copy, args);
    int length = std::vsnprintf(nullptr, 0, format, copy);
    va_end(copy);
    if (length < 0) {
        va_end(args);
        return 0;
    }
    std::vector<char> buffer(size_t(length) + 1);
    std::vsnprintf(buffer.data(), buffer.size(), format, args);
    va_end(args);
    return write(buffer.data(), size_t(length));
}

size_t Print::print(long value, int base) {
    if (base == 0)
        return write(uint8_t(value));
    return print(String(value, (unsigned char)base));
}

size_t Print::print(unsigned long value, int base) {
    if (base == 0)
        return write(uint8_t(value));
    return print(String(value, (unsigned char)base));
}

size_t Print::print(long long value, int base) { return print(String(value, (unsigned char)base)); }

size_t Print::print(unsigned long long value, int base) {
    return print(String(value, (unsigned char)base));
}

/* Arduino Print::printFloat: rounds by half of the last digit and limits the integer part. */
size_t Print::print(double value, int digits) {
    if (std::isnan(value))
        return print("nan");
    if (std::isinf(value))
        return print("inf");
    if (value > 4294967040.0 || value < -4294967040.0)
        return print("ovf");
    size_t written = 0;
    if (value < 0.0) {
        written += print('-');
        value = -value;
    }
    double half = 0.5;
    for (int i = 0; i < digits; i++)
        half /= 10.0;
    value += half;
    unsigned long whole = (unsigned long)value;
    double remainder = value - double(whole);
    written += print(whole);
    if (digits > 0)
        written += print('.');
    while (digits-- > 0) {
        remainder *= 10.0;
        unsigned int digit = (unsigned int)remainder;
        written += print(digit);
        remainder -= digit;
    }
    return written;
}

size_t Print::print(struct tm *time, const char *format) {
    char buffer[64];
    size_t length = std::strftime(buffer, sizeof(buffer), format ? format : "%c", time);
    return length ? write(buffer, length) : 0;
}
