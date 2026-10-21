/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include "Stream.h"

#include <cstdint>
#include <mutex>

/* I2C master forwarding every transaction to the shared C hardware model, using the same
 * event protocol as the QEMU adapter: Wire is model bus 0 and Wire1 is bus 1. */
class TwoWire : public Stream {
  public:
    explicit TwoWire(uint8_t bus) : bus(bus) {}
    bool begin(int sda = -1, int scl = -1, uint32_t frequency = 0);
    bool end();
    bool setClock(uint32_t frequency) {
        clock = frequency;
        return true;
    }
    uint32_t getClock() const { return clock; }
    void setTimeOut(uint16_t milliseconds) { timeout_ms = milliseconds; }
    uint16_t getTimeOut() const { return timeout_ms; }

    void beginTransmission(uint16_t address);
    void beginTransmission(uint8_t address) { beginTransmission(uint16_t(address)); }
    void beginTransmission(int address) { beginTransmission(uint16_t(address)); }
    uint8_t endTransmission(bool send_stop);
    uint8_t endTransmission() { return endTransmission(true); }
    size_t requestFrom(uint16_t address, size_t size, bool send_stop);
    uint8_t requestFrom(uint8_t address, uint8_t size) {
        return uint8_t(requestFrom(uint16_t(address), size_t(size), true));
    }
    uint8_t requestFrom(uint16_t address, uint8_t size) {
        return uint8_t(requestFrom(address, size_t(size), true));
    }
    uint8_t requestFrom(int address, int size) {
        return uint8_t(requestFrom(uint16_t(address), size_t(size), true));
    }
    uint8_t requestFrom(uint8_t address, uint8_t size, uint8_t send_stop) {
        return uint8_t(requestFrom(uint16_t(address), size_t(size), send_stop != 0));
    }

    using Print::write;
    size_t write(uint8_t value) override;
    size_t write(const uint8_t *data, size_t size) override;
    int available() override;
    int read() override;
    int peek() override;
    void flush() override;

  private:
    static constexpr size_t kBufferSize = 128;
    uint8_t bus;
    bool started = false, transmitting = false;
    uint16_t target = 0, timeout_ms = 50;
    uint32_t clock = 100000;
    uint8_t tx[kBufferSize] = {}, rx[kBufferSize] = {};
    size_t tx_length = 0, rx_length = 0, rx_index = 0;
    std::recursive_mutex lock;
};

extern TwoWire Wire;
extern TwoWire Wire1;
