/* SPDX-License-Identifier: GPL-2.0-only */
#include "Wire.h"

#include "Arduino.h"
#include "host.h"
#include "vclock.h"

extern "C" {
#include "model.h"
}

TwoWire Wire(0);
TwoWire Wire1(1);

namespace {

/* QEMU picsimlab i2c_event kinds; data bytes travel in the high byte of a SEND event. */
enum : uint16_t { kStartRecv = 0, kStartSend = 1, kFinish = 3, kNack = 4, kSend = 5, kRecv = 6 };

int event(uint8_t bus, uint16_t address, uint16_t kind) {
    int result = fs_model_event(bus, uint8_t(address), kind, flapsim::vclock::now_ns());
    flapsim::host::check_model();
    return result;
}

} // namespace

bool TwoWire::begin(int, int, uint32_t frequency) {
    std::lock_guard<std::recursive_mutex> guard(lock);
    if (frequency)
        clock = frequency;
    started = true;
    return true;
}

bool TwoWire::end() {
    std::lock_guard<std::recursive_mutex> guard(lock);
    started = false;
    return true;
}

void TwoWire::beginTransmission(uint16_t address) {
    std::lock_guard<std::recursive_mutex> guard(lock);
    target = address;
    tx_length = 0;
    transmitting = true;
}

size_t TwoWire::write(uint8_t value) {
    std::lock_guard<std::recursive_mutex> guard(lock);
    if (!transmitting || tx_length >= kBufferSize)
        return 0;
    tx[tx_length++] = value;
    return 1;
}

size_t TwoWire::write(const uint8_t *data, size_t size) {
    size_t written = 0;
    while (written < size && write(data[written]))
        written++;
    return written;
}

/* Return codes follow arduino-esp32 2.0.17: 0 ACK, 2 address NACK, 4 not initialized. */
uint8_t TwoWire::endTransmission(bool) {
    std::lock_guard<std::recursive_mutex> guard(lock);
    if (!started) {
        flapsim_log('E', "Wire.cpp", 448, "endTransmission", "NULL TX buffer pointer");
        return 4;
    }
    transmitting = false;
    std::lock_guard<std::timed_mutex> model(flapsim::host::model_lock());
    if (event(bus, target, kStartSend) != 0) {
        event(bus, target, kFinish);
        return 2;
    }
    for (size_t i = 0; i < tx_length; i++)
        event(bus, target, uint16_t(kSend | (tx[i] << 8)));
    event(bus, target, kFinish);
    tx_length = 0;
    return 0;
}

size_t TwoWire::requestFrom(uint16_t address, size_t size, bool) {
    std::lock_guard<std::recursive_mutex> guard(lock);
    rx_length = rx_index = 0;
    if (!started) {
        flapsim_log('E', "Wire.cpp", 481, "requestFrom", "NULL buffer pointer");
        return 0;
    }
    size = std::min(size, kBufferSize);
    std::lock_guard<std::timed_mutex> model(flapsim::host::model_lock());
    if (event(bus, address, kStartRecv) != 0) {
        event(bus, address, kFinish);
        flapsim_log('E', "Wire.cpp", 513, "requestFrom", "i2cRead returned Error %d", -1);
        return 0;
    }
    for (size_t i = 0; i < size; i++)
        rx[rx_length++] = uint8_t(event(bus, address, kRecv));
    event(bus, address, kNack);
    event(bus, address, kFinish);
    return rx_length;
}

int TwoWire::available() {
    std::lock_guard<std::recursive_mutex> guard(lock);
    return int(rx_length - rx_index);
}

int TwoWire::read() {
    std::lock_guard<std::recursive_mutex> guard(lock);
    return rx_index < rx_length ? rx[rx_index++] : -1;
}

int TwoWire::peek() {
    std::lock_guard<std::recursive_mutex> guard(lock);
    return rx_index < rx_length ? rx[rx_index] : -1;
}

void TwoWire::flush() {
    std::lock_guard<std::recursive_mutex> guard(lock);
    rx_length = rx_index = tx_length = 0;
}
