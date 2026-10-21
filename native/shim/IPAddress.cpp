/* SPDX-License-Identifier: GPL-2.0-only */
#include "IPAddress.h"

#include <arpa/inet.h>
#include <cstdio>
#include <cstring>

IPAddress::IPAddress(uint32_t address) { std::memcpy(bytes, &address, sizeof(bytes)); }

IPAddress::operator uint32_t() const {
    uint32_t address;
    std::memcpy(&address, bytes, sizeof(address));
    return address;
}

bool IPAddress::fromString(const char *text) {
    in_addr parsed;
    if (!text || inet_pton(AF_INET, text, &parsed) != 1)
        return false;
    std::memcpy(bytes, &parsed.s_addr, sizeof(bytes));
    return true;
}

String IPAddress::toString() const {
    char text[16];
    std::snprintf(text, sizeof(text), "%u.%u.%u.%u", bytes[0], bytes[1], bytes[2], bytes[3]);
    return String(text);
}
