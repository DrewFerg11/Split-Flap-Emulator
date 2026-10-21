/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include "Client.h"

/* TCP client. This phase has no host network path yet, so connections are refused. */
class WiFiClient : public Client {
  public:
    int connect(IPAddress, uint16_t) override { return 0; }
    int connect(const char *, uint16_t) override { return 0; }
    using Print::write;
    size_t write(uint8_t) override { return 0; }
    size_t write(const uint8_t *, size_t) override { return 0; }
    int available() override { return 0; }
    int read() override { return -1; }
    int read(uint8_t *, size_t) override { return -1; }
    int peek() override { return -1; }
    void flush() override {}
    void stop() override {}
    uint8_t connected() override { return 0; }
    operator bool() override { return false; }
};
