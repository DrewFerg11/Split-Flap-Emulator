/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include "IPAddress.h"
#include "Stream.h"

class Client : public Stream {
  public:
    virtual int connect(IPAddress ip, uint16_t port) = 0;
    virtual int connect(const char *host, uint16_t port) = 0;
    using Print::write;
    size_t write(uint8_t value) override = 0;
    size_t write(const uint8_t *buffer, size_t size) override = 0;
    int available() override = 0;
    int read() override = 0;
    virtual int read(uint8_t *buffer, size_t size) = 0;
    int peek() override = 0;
    void flush() override = 0;
    virtual void stop() = 0;
    virtual uint8_t connected() = 0;
    virtual operator bool() = 0;

  protected:
    uint8_t *rawIPAddress(IPAddress &address) { return address.raw_address(); }
};
