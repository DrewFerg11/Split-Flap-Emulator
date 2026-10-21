/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include "WString.h"
#include "nvs.h"

#include <atomic>
#include <cstddef>
#include <cstdint>

typedef enum {
    PT_I8,
    PT_U8,
    PT_I16,
    PT_U16,
    PT_I32,
    PT_U32,
    PT_I64,
    PT_U64,
    PT_STR,
    PT_BLOB,
    PT_INVALID
} PreferenceType;

/* Behaviour of arduino-esp32 2.0.17 Preferences, including its unsynchronized started/handle
 * state (shared between tasks it races exactly as on the device) and its error log lines. */
class Preferences {
  public:
    ~Preferences() { end(); }
    bool begin(const char *name, bool read_only = false, const char *partition_label = nullptr);
    void end();
    bool clear();
    bool remove(const char *key);

    size_t putChar(const char *key, int8_t value);
    size_t putUChar(const char *key, uint8_t value);
    size_t putShort(const char *key, int16_t value);
    size_t putUShort(const char *key, uint16_t value);
    size_t putInt(const char *key, int32_t value);
    size_t putUInt(const char *key, uint32_t value);
    size_t putLong(const char *key, int32_t value) { return putInt(key, value); }
    size_t putULong(const char *key, uint32_t value) { return putUInt(key, value); }
    size_t putLong64(const char *key, int64_t value);
    size_t putULong64(const char *key, uint64_t value);
    size_t putFloat(const char *key, float value) { return putBytes(key, &value, sizeof(value)); }
    size_t putDouble(const char *key, double value) { return putBytes(key, &value, sizeof(value)); }
    size_t putBool(const char *key, bool value) { return putUChar(key, value ? 1 : 0); }
    size_t putString(const char *key, const char *value);
    size_t putString(const char *key, const String value) { return putString(key, value.c_str()); }
    size_t putBytes(const char *key, const void *value, size_t length);

    bool isKey(const char *key) { return getType(key) != PT_INVALID; }
    PreferenceType getType(const char *key);
    int8_t getChar(const char *key, int8_t fallback = 0);
    uint8_t getUChar(const char *key, uint8_t fallback = 0);
    int16_t getShort(const char *key, int16_t fallback = 0);
    uint16_t getUShort(const char *key, uint16_t fallback = 0);
    int32_t getInt(const char *key, int32_t fallback = 0);
    uint32_t getUInt(const char *key, uint32_t fallback = 0);
    int32_t getLong(const char *key, int32_t fallback = 0) { return getInt(key, fallback); }
    uint32_t getULong(const char *key, uint32_t fallback = 0) { return getUInt(key, fallback); }
    int64_t getLong64(const char *key, int64_t fallback = 0);
    uint64_t getULong64(const char *key, uint64_t fallback = 0);
    float getFloat(const char *key, float fallback = NAN_DEFAULT_FLOAT);
    double getDouble(const char *key, double fallback = NAN_DEFAULT_FLOAT);
    bool getBool(const char *key, bool fallback = false) {
        return getUChar(key, fallback ? 1 : 0) == 1;
    }
    size_t getString(const char *key, char *value, size_t max_length);
    String getString(const char *key, String fallback = String());
    size_t getBytesLength(const char *key);
    size_t getBytes(const char *key, void *buffer, size_t max_length);
    size_t freeEntries() { return 500; }

  private:
    static constexpr float NAN_DEFAULT_FLOAT = __builtin_nanf("");
    template <typename T>
    size_t putNumber(const char *key, T value, NvsType type, unsigned line, unsigned commit_line,
                     const char *function, const char *operation);
    template <typename T> T getNumber(const char *key, T fallback, NvsType type);
    std::atomic<nvs_handle_t> handle{0};
    std::atomic<bool> started{false}, read_only{false};
};
