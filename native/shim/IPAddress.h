/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include "WString.h"

#include <cstdint>

class IPAddress {
  public:
    IPAddress() = default;
    IPAddress(uint8_t a, uint8_t b, uint8_t c, uint8_t d) : bytes{a, b, c, d} {}
    /* Network byte order in memory, as lwIP stores IPv4 addresses. */
    IPAddress(uint32_t address);
    explicit IPAddress(const uint8_t *address)
        : IPAddress(address[0], address[1], address[2], address[3]) {}
    operator uint32_t() const;
    bool operator==(const IPAddress &other) const { return uint32_t(*this) == uint32_t(other); }
    bool operator!=(const IPAddress &other) const { return !(*this == other); }
    uint8_t operator[](int index) const { return bytes[index]; }
    uint8_t &operator[](int index) { return bytes[index]; }
    bool fromString(const char *text);
    bool fromString(const String &text) { return fromString(text.c_str()); }
    String toString() const;
    uint8_t *raw_address() { return bytes; }

  private:
    uint8_t bytes[4] = {0, 0, 0, 0};
};
