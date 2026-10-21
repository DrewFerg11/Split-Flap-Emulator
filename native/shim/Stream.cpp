/* SPDX-License-Identifier: GPL-2.0-only */
#include "Arduino.h"

int Stream::timedRead() {
    unsigned long start = millis();
    do {
        int value = read();
        if (value >= 0)
            return value;
        delay(1);
    } while (millis() - start < timeout);
    return -1;
}

size_t Stream::readBytes(char *buffer, size_t length) {
    size_t count = 0;
    while (count < length) {
        int value = timedRead();
        if (value < 0)
            break;
        buffer[count++] = char(value);
    }
    return count;
}

size_t Stream::readBytesUntil(char terminator, char *buffer, size_t length) {
    size_t count = 0;
    while (count < length) {
        int value = timedRead();
        if (value < 0 || value == terminator)
            break;
        buffer[count++] = char(value);
    }
    return count;
}

String Stream::readString() {
    String result;
    for (int value; (value = timedRead()) >= 0;)
        result += char(value);
    return result;
}

String Stream::readStringUntil(char terminator) {
    String result;
    for (int value; (value = timedRead()) >= 0 && value != terminator;)
        result += char(value);
    return result;
}
