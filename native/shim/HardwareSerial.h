/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include "Stream.h"

/* UART0: output is appended to serial.log; input is read from the run's optional serial pipe. */
class HardwareSerial : public Stream {
  public:
    void begin(unsigned long baud, uint32_t config = 0, int8_t rx = -1, int8_t tx = -1,
               bool invert = false, unsigned long timeout = 20000, uint8_t threshold = 112);
    void end() {}
    int available() override;
    int read() override;
    int peek() override;
    using Print::write;
    size_t write(uint8_t value) override { return write(&value, 1); }
    size_t write(const uint8_t *buffer, size_t size) override;
    void flush() override {}
    unsigned long baudRate() const { return baud; }
    operator bool() const { return true; }

  private:
    unsigned long baud = 0;
};

extern HardwareSerial Serial;
