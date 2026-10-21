/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include "esp_system.h"

#include <cstddef>
#include <cstdint>
#include <string>

/* ESP-IDF NVS semantics over a JSON file: typed keys in namespaces, handles that become invalid
 * once closed, read-only opens that cannot create a namespace, and a commit that persists. */
#define ESP_ERR_NVS_BASE 0x1100
#define ESP_ERR_NVS_NOT_INITIALIZED (ESP_ERR_NVS_BASE + 0x01)
#define ESP_ERR_NVS_NOT_FOUND (ESP_ERR_NVS_BASE + 0x02)
#define ESP_ERR_NVS_TYPE_MISMATCH (ESP_ERR_NVS_BASE + 0x03)
#define ESP_ERR_NVS_READ_ONLY (ESP_ERR_NVS_BASE + 0x04)
#define ESP_ERR_NVS_NOT_ENOUGH_SPACE (ESP_ERR_NVS_BASE + 0x05)
#define ESP_ERR_NVS_INVALID_NAME (ESP_ERR_NVS_BASE + 0x06)
#define ESP_ERR_NVS_INVALID_HANDLE (ESP_ERR_NVS_BASE + 0x07)
#define ESP_ERR_NVS_REMOVE_FAILED (ESP_ERR_NVS_BASE + 0x08)
#define ESP_ERR_NVS_KEY_TOO_LONG (ESP_ERR_NVS_BASE + 0x09)
#define ESP_ERR_NVS_PAGE_FULL (ESP_ERR_NVS_BASE + 0x0a)
#define ESP_ERR_NVS_INVALID_STATE (ESP_ERR_NVS_BASE + 0x0b)
#define ESP_ERR_NVS_INVALID_LENGTH (ESP_ERR_NVS_BASE + 0x0c)
#define ESP_ERR_NVS_VALUE_TOO_LONG (ESP_ERR_NVS_BASE + 0x0e)

typedef uint32_t nvs_handle_t;
enum class NvsType { I8, U8, I16, U16, I32, U32, I64, U64, Str, Blob };

namespace flapsim::nvs {

const char *error_name(esp_err_t error);
esp_err_t open(const char *name, bool read_only, nvs_handle_t *handle);
void close(nvs_handle_t handle);
esp_err_t set(nvs_handle_t handle, const char *key, NvsType type, const std::string &bytes);
/* `bytes` receives the stored value; strings include no terminator. */
esp_err_t get(nvs_handle_t handle, const char *key, NvsType type, std::string *bytes);
esp_err_t erase_key(nvs_handle_t handle, const char *key);
esp_err_t erase_all(nvs_handle_t handle);
esp_err_t commit(nvs_handle_t handle);
/* The type of an existing key, for Preferences::getType/isKey. */
esp_err_t find(nvs_handle_t handle, const char *key, NvsType *type);

} // namespace flapsim::nvs
