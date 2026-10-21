/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include <cstdint>

class EspClass {
  public:
    [[noreturn]] void restart();
    const char *getChipModel() { return "ESP32-D0WDQ6"; }
    uint8_t getChipRevision() { return 0; }
    uint8_t getChipCores() { return 2; }
    uint32_t getCpuFreqMHz() { return 240; }
    uint32_t getFlashChipSize() { return 4 * 1024 * 1024; }
    uint32_t getHeapSize() { return 327680; }
    uint32_t getFreeHeap() { return 200000; }
    uint32_t getMinFreeHeap() { return 180000; }
    uint32_t getMaxAllocHeap() { return 110000; }
    uint64_t getEfuseMac() { return 0x0000240AC4000110ULL; }
    const char *getSdkVersion() { return "flapsim-native"; }
};

extern EspClass ESP;
