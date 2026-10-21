/* SPDX-License-Identifier: GPL-2.0-only */
#include "nvs.h"

#include "host.h"

#include <cerrno>
#include <cinttypes>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <json.h>
#include <map>
#include <mutex>
#include <unistd.h>

namespace flapsim::nvs {
namespace {

constexpr size_t kMaxName = 15;
constexpr size_t kMaxString = 4000;
constexpr size_t kMaxBlob = 508000;

struct Item {
    NvsType type;
    std::string bytes;
};
struct Handle {
    std::string space;
    bool read_only;
};

std::mutex mutex;
bool loaded = false;
std::map<std::string, std::map<std::string, Item>> spaces;
std::map<nvs_handle_t, Handle> handles;
nvs_handle_t next_handle = 1;

const char *type_names[] = {"i8", "u8", "i16", "u16", "i32", "u32", "i64", "u64", "str", "blob"};

size_t width(NvsType type) {
    switch (type) {
    case NvsType::I8:
    case NvsType::U8:
        return 1;
    case NvsType::I16:
    case NvsType::U16:
        return 2;
    case NvsType::I32:
    case NvsType::U32:
        return 4;
    case NvsType::I64:
    case NvsType::U64:
        return 8;
    default:
        return 0;
    }
}

bool is_signed(NvsType type) {
    return type == NvsType::I8 || type == NvsType::I16 || type == NvsType::I32 ||
           type == NvsType::I64;
}

[[noreturn]] void corrupt(const char *why) {
    std::fprintf(stderr, "flapsim-native: invalid NVS file %s: %s\n", host::config().nvs.c_str(),
                 why);
    std::exit(3);
}

std::string hex(const std::string &bytes) {
    static const char digits[] = "0123456789abcdef";
    std::string out;
    for (unsigned char c : bytes) {
        out += digits[c >> 4];
        out += digits[c & 15];
    }
    return out;
}

std::string unhex(const char *text) {
    std::string out;
    size_t length = std::strlen(text);
    if (length % 2)
        corrupt("odd-length blob");
    for (size_t i = 0; i < length; i += 2) {
        char pair[3] = {text[i], text[i + 1], 0};
        char *end;
        long value = std::strtol(pair, &end, 16);
        if (*end)
            corrupt("non-hex blob");
        out += char(value);
    }
    return out;
}

json_object *encode(const Item &item) {
    json_object *entry = json_object_new_object();
    json_object_object_add(entry, "type", json_object_new_string(type_names[int(item.type)]));
    json_object *value;
    if (item.type == NvsType::Str) {
        value = json_object_new_string_len(item.bytes.data(), int(item.bytes.size()));
    } else if (item.type == NvsType::Blob) {
        value = json_object_new_string(hex(item.bytes).c_str());
    } else {
        uint64_t raw = 0;
        std::memcpy(&raw, item.bytes.data(), width(item.type));
        if (is_signed(item.type)) {
            int shift = 64 - int(width(item.type)) * 8;
            value = json_object_new_int64(int64_t(raw << shift) >> shift);
        } else {
            value = json_object_new_uint64(raw);
        }
    }
    json_object_object_add(entry, "value", value);
    return entry;
}

Item decode(json_object *entry) {
    json_object *type_value, *value;
    if (!json_object_is_type(entry, json_type_object) ||
        !json_object_object_get_ex(entry, "type", &type_value) ||
        !json_object_object_get_ex(entry, "value", &value))
        corrupt("entries need type and value");
    const char *name = json_object_get_string(type_value);
    int index = 0;
    while (index < 10 && std::strcmp(type_names[index], name ? name : ""))
        index++;
    if (index == 10)
        corrupt("unknown entry type");
    Item item{NvsType(index), {}};
    if (item.type == NvsType::Str || item.type == NvsType::Blob) {
        if (!json_object_is_type(value, json_type_string))
            corrupt("string and blob values must be JSON strings");
        item.bytes = item.type == NvsType::Str ? std::string(json_object_get_string(value),
                                                             json_object_get_string_len(value))
                                               : unhex(json_object_get_string(value));
    } else {
        if (!json_object_is_type(value, json_type_int))
            corrupt("integer values must be JSON integers");
        uint64_t raw = is_signed(item.type) ? uint64_t(json_object_get_int64(value))
                                            : json_object_get_uint64(value);
        item.bytes.assign(reinterpret_cast<const char *>(&raw), width(item.type));
    }
    return item;
}

void load() {
    if (loaded)
        return;
    loaded = true;
    if (access(host::config().nvs.c_str(), F_OK) != 0)
        return;
    json_object *root = json_object_from_file(host::config().nvs.c_str());
    json_object *version, *names;
    if (!root || !json_object_object_get_ex(root, "schema_version", &version) ||
        json_object_get_int(version) != 1 ||
        !json_object_object_get_ex(root, "namespaces", &names) ||
        !json_object_is_type(names, json_type_object))
        corrupt("expected schema_version 1 and a namespaces object");
    json_object_object_foreach(names, space, keys) {
        if (!json_object_is_type(keys, json_type_object))
            corrupt("namespace values must be objects");
        auto &items = spaces[space];
        json_object_object_foreach(keys, key, entry) items[key] = decode(entry);
    }
    json_object_put(root);
}

/* Written through a temporary file, so an emulated power loss never leaves a torn store. */
void persist() {
    json_object *root = json_object_new_object();
    json_object_object_add(root, "schema_version", json_object_new_int(1));
    json_object *names = json_object_new_object();
    for (const auto &[space, items] : spaces) {
        json_object *keys = json_object_new_object();
        for (const auto &[key, item] : items)
            json_object_object_add(keys, key.c_str(), encode(item));
        json_object_object_add(names, space.c_str(), keys);
    }
    json_object_object_add(root, "namespaces", names);
    std::string temporary = host::config().nvs + ".tmp";
    if (json_object_to_file_ext(temporary.c_str(), root, JSON_C_TO_STRING_PRETTY) != 0 ||
        std::rename(temporary.c_str(), host::config().nvs.c_str()) != 0) {
        std::fprintf(stderr, "flapsim-native: cannot write NVS file: %s\n", std::strerror(errno));
        std::exit(3);
    }
    json_object_put(root);
}

Handle *lookup(nvs_handle_t handle) {
    auto found = handles.find(handle);
    return found == handles.end() ? nullptr : &found->second;
}

esp_err_t check_key(const char *key) {
    if (!key || !*key)
        return ESP_ERR_NVS_INVALID_NAME;
    return std::strlen(key) > kMaxName ? ESP_ERR_NVS_KEY_TOO_LONG : ESP_OK;
}

} // namespace

const char *error_name(esp_err_t error) {
    static const char *names[] = {"OTHER",         "NOT_INITIALIZED", "NOT_FOUND",
                                  "TYPE_MISMATCH", "READ_ONLY",       "NOT_ENOUGH_SPACE",
                                  "INVALID_NAME",  "INVALID_HANDLE",  "REMOVE_FAILED",
                                  "KEY_TOO_LONG",  "PAGE_FULL",       "INVALID_STATE",
                                  "INVALID_LENGTH"};
    if (error > ESP_ERR_NVS_BASE && error <= ESP_ERR_NVS_INVALID_LENGTH)
        return names[error & ~ESP_ERR_NVS_BASE];
    return names[0];
}

esp_err_t open(const char *name, bool read_only, nvs_handle_t *handle) {
    std::lock_guard<std::mutex> lock(mutex);
    load();
    if (esp_err_t error = check_key(name))
        return error;
    if (!spaces.count(name)) {
        if (read_only)
            return ESP_ERR_NVS_NOT_FOUND;
        spaces[name];
        persist();
    }
    *handle = next_handle++;
    handles[*handle] = Handle{name, read_only};
    return ESP_OK;
}

void close(nvs_handle_t handle) {
    std::lock_guard<std::mutex> lock(mutex);
    handles.erase(handle);
}

esp_err_t set(nvs_handle_t handle, const char *key, NvsType type, const std::string &bytes) {
    std::lock_guard<std::mutex> lock(mutex);
    Handle *open = lookup(handle);
    if (!open)
        return ESP_ERR_NVS_INVALID_HANDLE;
    if (open->read_only)
        return ESP_ERR_NVS_READ_ONLY;
    if (esp_err_t error = check_key(key))
        return error;
    if ((type == NvsType::Str && bytes.size() + 1 > kMaxString) ||
        (type == NvsType::Blob && bytes.size() > kMaxBlob))
        return ESP_ERR_NVS_VALUE_TOO_LONG;
    spaces[open->space][key] = Item{type, bytes};
    persist();
    return ESP_OK;
}

esp_err_t get(nvs_handle_t handle, const char *key, NvsType type, std::string *bytes) {
    std::lock_guard<std::mutex> lock(mutex);
    Handle *open = lookup(handle);
    if (!open)
        return ESP_ERR_NVS_INVALID_HANDLE;
    if (esp_err_t error = check_key(key))
        return error;
    auto &items = spaces[open->space];
    auto found = items.find(key);
    if (found == items.end() || found->second.type != type)
        return ESP_ERR_NVS_NOT_FOUND;
    *bytes = found->second.bytes;
    return ESP_OK;
}

esp_err_t find(nvs_handle_t handle, const char *key, NvsType *type) {
    std::lock_guard<std::mutex> lock(mutex);
    Handle *open = lookup(handle);
    if (!open)
        return ESP_ERR_NVS_INVALID_HANDLE;
    if (esp_err_t error = check_key(key))
        return error;
    auto &items = spaces[open->space];
    auto found = items.find(key);
    if (found == items.end())
        return ESP_ERR_NVS_NOT_FOUND;
    *type = found->second.type;
    return ESP_OK;
}

esp_err_t erase_key(nvs_handle_t handle, const char *key) {
    std::lock_guard<std::mutex> lock(mutex);
    Handle *open = lookup(handle);
    if (!open)
        return ESP_ERR_NVS_INVALID_HANDLE;
    if (open->read_only)
        return ESP_ERR_NVS_READ_ONLY;
    if (esp_err_t error = check_key(key))
        return error;
    if (!spaces[open->space].erase(key))
        return ESP_ERR_NVS_NOT_FOUND;
    persist();
    return ESP_OK;
}

esp_err_t erase_all(nvs_handle_t handle) {
    std::lock_guard<std::mutex> lock(mutex);
    Handle *open = lookup(handle);
    if (!open)
        return ESP_ERR_NVS_INVALID_HANDLE;
    if (open->read_only)
        return ESP_ERR_NVS_READ_ONLY;
    spaces[open->space].clear();
    persist();
    return ESP_OK;
}

esp_err_t commit(nvs_handle_t handle) {
    std::lock_guard<std::mutex> lock(mutex);
    return lookup(handle) ? ESP_OK : ESP_ERR_NVS_INVALID_HANDLE;
}

} // namespace flapsim::nvs
