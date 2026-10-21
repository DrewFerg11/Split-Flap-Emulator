/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include "WString.h"

#include <cstdint>

/* No multicast DNS on the host: the responder accepts its name and services and stays silent. */
class MDNSResponder {
  public:
    bool begin(const char *hostname) { return hostname && *hostname; }
    void end() {}
    bool addService(const char *, const char *, uint16_t) { return true; }
    bool addService(const String &, const String &, uint16_t) { return true; }
    void setInstanceName(const char *) {}
};

extern MDNSResponder MDNS;
