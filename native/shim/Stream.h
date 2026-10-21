/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include "Print.h"

class Stream : public Print {
  public:
    virtual int available() = 0;
    virtual int read() = 0;
    virtual int peek() = 0;

    void setTimeout(unsigned long milliseconds) { timeout = milliseconds; }
    unsigned long getTimeout() const { return timeout; }
    virtual size_t readBytes(char *buffer, size_t length);
    size_t readBytes(uint8_t *buffer, size_t length) {
        return readBytes(reinterpret_cast<char *>(buffer), length);
    }
    size_t readBytesUntil(char terminator, char *buffer, size_t length);
    String readString();
    String readStringUntil(char terminator);

  protected:
    int timedRead();
    unsigned long timeout = 1000;
};
