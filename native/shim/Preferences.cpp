/* SPDX-License-Identifier: GPL-2.0-only */
#include "Preferences.h"

#include "esp32-hal-log.h"

#include <cstring>

using flapsim::nvs::error_name;

/* Source locations of the corresponding arduino-esp32 2.0.17 log_e calls. */
#define PREFERENCES_LOG(line, function, ...)                                                       \
    flapsim_log('E', "Preferences.cpp", line, function, __VA_ARGS__)

bool Preferences::begin(const char *name, bool read_only_open, const char *partition_label) {
    if (started)
        return false;
    read_only = read_only_open;
    if (partition_label) {
        PREFERENCES_LOG(42, "begin", "nvs_flash_init_partition failed: %s",
                        error_name(ESP_ERR_NVS_NOT_FOUND));
        return false;
    }
    nvs_handle_t opened = 0;
    esp_err_t error = flapsim::nvs::open(name, read_only_open, &opened);
    if (error) {
        PREFERENCES_LOG(50, "begin", "nvs_open failed: %s", error_name(error));
        return false;
    }
    handle = opened;
    started = true;
    return true;
}

void Preferences::end() {
    if (!started)
        return;
    flapsim::nvs::close(handle);
    started = false;
}

bool Preferences::clear() {
    if (!started || read_only)
        return false;
    esp_err_t error = flapsim::nvs::erase_all(handle);
    if (error) {
        PREFERENCES_LOG(75, "clear", "nvs_erase_all fail: %s", error_name(error));
        return false;
    }
    if ((error = flapsim::nvs::commit(handle))) {
        PREFERENCES_LOG(80, "clear", "nvs_commit fail: %s", error_name(error));
        return false;
    }
    return true;
}

bool Preferences::remove(const char *key) {
    if (!started || !key || read_only)
        return false;
    esp_err_t error = flapsim::nvs::erase_key(handle, key);
    if (error) {
        PREFERENCES_LOG(96, "remove", "nvs_erase_key fail: %s %s", key, error_name(error));
        return false;
    }
    if ((error = flapsim::nvs::commit(handle))) {
        PREFERENCES_LOG(101, "remove", "nvs_commit fail: %s %s", key, error_name(error));
        return false;
    }
    return true;
}

template <typename T>
size_t Preferences::putNumber(const char *key, T value, NvsType type, unsigned line,
                              unsigned commit_line, const char *function, const char *operation) {
    if (!started || !key || read_only)
        return 0;
    std::string bytes(reinterpret_cast<const char *>(&value), sizeof(value));
    esp_err_t error = flapsim::nvs::set(handle, key, type, bytes);
    if (error) {
        PREFERENCES_LOG(line, function, "%s fail: %s %s", operation, key, error_name(error));
        return 0;
    }
    if ((error = flapsim::nvs::commit(handle))) {
        PREFERENCES_LOG(commit_line, function, "nvs_commit fail: %s %s", key, error_name(error));
        return 0;
    }
    return sizeof(value);
}

size_t Preferences::putChar(const char *key, int8_t value) {
    return putNumber(key, value, NvsType::I8, 117, 122, "putChar", "nvs_set_i8");
}
size_t Preferences::putUChar(const char *key, uint8_t value) {
    return putNumber(key, value, NvsType::U8, 134, 139, "putUChar", "nvs_set_u8");
}
size_t Preferences::putShort(const char *key, int16_t value) {
    return putNumber(key, value, NvsType::I16, 151, 156, "putShort", "nvs_set_i16");
}
size_t Preferences::putUShort(const char *key, uint16_t value) {
    return putNumber(key, value, NvsType::U16, 168, 173, "putUShort", "nvs_set_u16");
}
size_t Preferences::putInt(const char *key, int32_t value) {
    return putNumber(key, value, NvsType::I32, 185, 190, "putInt", "nvs_set_i32");
}
size_t Preferences::putUInt(const char *key, uint32_t value) {
    return putNumber(key, value, NvsType::U32, 202, 207, "putUInt", "nvs_set_u32");
}
size_t Preferences::putLong64(const char *key, int64_t value) {
    return putNumber(key, value, NvsType::I64, 227, 232, "putLong64", "nvs_set_i64");
}
size_t Preferences::putULong64(const char *key, uint64_t value) {
    return putNumber(key, value, NvsType::U64, 244, 249, "putULong64", "nvs_set_u64");
}

size_t Preferences::putString(const char *key, const char *value) {
    if (!started || !key || !value || read_only)
        return 0;
    esp_err_t error = flapsim::nvs::set(handle, key, NvsType::Str, value);
    if (error) {
        PREFERENCES_LOG(273, "putString", "nvs_set_str fail: %s %s", key, error_name(error));
        return 0;
    }
    if ((error = flapsim::nvs::commit(handle))) {
        PREFERENCES_LOG(278, "putString", "nvs_commit fail: %s %s", key, error_name(error));
        return 0;
    }
    return std::strlen(value);
}

size_t Preferences::putBytes(const char *key, const void *value, size_t length) {
    if (!started || !key || !value || !length || read_only)
        return 0;
    esp_err_t error = flapsim::nvs::set(handle, key, NvsType::Blob,
                                        std::string(static_cast<const char *>(value), length));
    if (error) {
        PREFERENCES_LOG(294, "putBytes", "nvs_set_blob fail: %s %s", key, error_name(error));
        return 0;
    }
    if ((error = flapsim::nvs::commit(handle))) {
        PREFERENCES_LOG(299, "putBytes", "nvs_commit fail: %s %s", key, error_name(error));
        return 0;
    }
    return length;
}

PreferenceType Preferences::getType(const char *key) {
    if (!started || !key || std::strlen(key) > 15)
        return PT_INVALID;
    NvsType type;
    if (flapsim::nvs::find(handle, key, &type))
        return PT_INVALID;
    return PreferenceType(int(type));
}

/* Integer reads log only at verbose level in the core, which release builds do not print. */
template <typename T> T Preferences::getNumber(const char *key, T fallback, NvsType type) {
    if (!started || !key)
        return fallback;
    std::string bytes;
    if (flapsim::nvs::get(handle, key, type, &bytes) || bytes.size() != sizeof(T))
        return fallback;
    T value;
    std::memcpy(&value, bytes.data(), sizeof(T));
    return value;
}

int8_t Preferences::getChar(const char *key, int8_t fallback) {
    return getNumber(key, fallback, NvsType::I8);
}
uint8_t Preferences::getUChar(const char *key, uint8_t fallback) {
    return getNumber(key, fallback, NvsType::U8);
}
int16_t Preferences::getShort(const char *key, int16_t fallback) {
    return getNumber(key, fallback, NvsType::I16);
}
uint16_t Preferences::getUShort(const char *key, uint16_t fallback) {
    return getNumber(key, fallback, NvsType::U16);
}
int32_t Preferences::getInt(const char *key, int32_t fallback) {
    return getNumber(key, fallback, NvsType::I32);
}
uint32_t Preferences::getUInt(const char *key, uint32_t fallback) {
    return getNumber(key, fallback, NvsType::U32);
}
int64_t Preferences::getLong64(const char *key, int64_t fallback) {
    return getNumber(key, fallback, NvsType::I64);
}
uint64_t Preferences::getULong64(const char *key, uint64_t fallback) {
    return getNumber(key, fallback, NvsType::U64);
}

float Preferences::getFloat(const char *key, float fallback) {
    float value = fallback;
    getBytes(key, &value, sizeof(value));
    return value;
}

double Preferences::getDouble(const char *key, double fallback) {
    double value = fallback;
    getBytes(key, &value, sizeof(value));
    return value;
}

size_t Preferences::getString(const char *key, char *value, size_t max_length) {
    if (!started || !key || !value || !max_length)
        return 0;
    std::string bytes;
    esp_err_t error = flapsim::nvs::get(handle, key, NvsType::Str, &bytes);
    if (error) {
        PREFERENCES_LOG(460, "getString", "nvs_get_str len fail: %s %s", key, error_name(error));
        return 0;
    }
    size_t length = bytes.size() + 1;
    if (length > max_length) {
        PREFERENCES_LOG(464, "getString", "not enough space in value: %u < %u",
                        unsigned(max_length), unsigned(length));
        return 0;
    }
    if ((error = flapsim::nvs::get(handle, key, NvsType::Str, &bytes)) ||
        bytes.size() + 1 != length) {
        PREFERENCES_LOG(469, "getString", "nvs_get_str fail: %s %s", key,
                        error_name(error ? error : ESP_ERR_NVS_INVALID_LENGTH));
        return 0;
    }
    std::memcpy(value, bytes.c_str(), length);
    return length;
}

String Preferences::getString(const char *key, const String fallback) {
    if (!started || !key)
        return fallback;
    std::string bytes;
    esp_err_t error = flapsim::nvs::get(handle, key, NvsType::Str, &bytes);
    if (error) {
        PREFERENCES_LOG(483, "getString", "nvs_get_str len fail: %s %s", key, error_name(error));
        return fallback;
    }
    /* The core reads the length and the value in two calls; keep that window for races. */
    if ((error = flapsim::nvs::get(handle, key, NvsType::Str, &bytes))) {
        PREFERENCES_LOG(490, "getString", "nvs_get_str fail: %s %s", key, error_name(error));
        return fallback;
    }
    return String(bytes.c_str());
}

size_t Preferences::getBytesLength(const char *key) {
    if (!started || !key)
        return 0;
    std::string bytes;
    esp_err_t error = flapsim::nvs::get(handle, key, NvsType::Blob, &bytes);
    if (error) {
        PREFERENCES_LOG(503, "getBytesLength", "nvs_get_blob len fail: %s %s", key,
                        error_name(error));
        return 0;
    }
    return bytes.size();
}

size_t Preferences::getBytes(const char *key, void *buffer, size_t max_length) {
    size_t length = getBytesLength(key);
    if (!length || !buffer || !max_length)
        return length;
    if (length > max_length) {
        PREFERENCES_LOG(515, "getBytes", "not enough space in buffer: %u < %u",
                        unsigned(max_length), unsigned(length));
        return 0;
    }
    std::string bytes;
    esp_err_t error = flapsim::nvs::get(handle, key, NvsType::Blob, &bytes);
    if (error) {
        PREFERENCES_LOG(520, "getBytes", "nvs_get_blob fail: %s %s", key, error_name(error));
        return 0;
    }
    std::memcpy(buffer, bytes.data(), bytes.size());
    return bytes.size();
}
