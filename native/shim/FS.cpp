/* SPDX-License-Identifier: GPL-2.0-only */
#include "FS.h"

#include "LittleFS.h"
#include "esp32-hal-log.h"
#include "host.h"

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <dirent.h>
#include <sys/stat.h>
#include <unistd.h>

LittleFSFS LittleFS;

namespace fs {

FileImpl::~FileImpl() {
    if (file)
        std::fclose(file);
}

size_t File::write(const uint8_t *buffer, size_t size) {
    if (!impl || !impl->file)
        return 0;
    return std::fwrite(buffer, 1, size, impl->file);
}

int File::available() {
    if (!impl || !impl->file)
        return 0;
    return int(size() - position());
}

int File::read() {
    uint8_t value;
    return read(&value, 1) == 1 ? value : -1;
}

size_t File::read(uint8_t *buffer, size_t size) {
    if (!impl || !impl->file)
        return 0;
    return std::fread(buffer, 1, size, impl->file);
}

int File::peek() {
    if (!impl || !impl->file)
        return -1;
    int value = std::fgetc(impl->file);
    if (value != EOF)
        std::ungetc(value, impl->file);
    return value == EOF ? -1 : value;
}

void File::flush() {
    if (impl && impl->file)
        std::fflush(impl->file);
}

bool File::seek(uint32_t offset, SeekMode mode) {
    if (!impl || !impl->file)
        return false;
    int whence = mode == SeekSet ? SEEK_SET : mode == SeekCur ? SEEK_CUR : SEEK_END;
    return std::fseek(impl->file, long(offset), whence) == 0;
}

size_t File::position() const {
    if (!impl || !impl->file)
        return 0;
    long value = std::ftell(impl->file);
    return value < 0 ? 0 : size_t(value);
}

size_t File::size() const {
    if (!impl || impl->directory)
        return 0;
    if (impl->file)
        std::fflush(impl->file);
    struct stat status;
    return ::stat(impl->host_path.c_str(), &status) == 0 ? size_t(status.st_size) : 0;
}

/* arduino-esp32 2.x returns the last path component. */
const char *File::name() const {
    if (!impl)
        return nullptr;
    const char *slash = std::strrchr(impl->path.c_str(), '/');
    return slash ? slash + 1 : impl->path.c_str();
}

File File::openNextFile(const char *mode) {
    if (!impl || !impl->directory || !owner || impl->next_entry >= impl->entries.size())
        return File();
    std::string child = impl->path == "/" ? "/" : impl->path + "/";
    child += impl->entries[impl->next_entry++];
    return owner->open(child.c_str(), mode);
}

/* Error log locations follow arduino-esp32 2.0.17 vfs_api.cpp. */
std::string FS::resolve(const char *path, const char *function, unsigned line) {
    if (!mounted) {
        flapsim_log('E', "vfs_api.cpp", line, function, "File system is not mounted");
        return "";
    }
    if (!path || path[0] != '/') {
        flapsim_log('E', "vfs_api.cpp", line + 5, function, "%s does not start with /",
                    path ? path : "");
        return "";
    }
    std::string value = path;
    if (value.find("/../") != std::string::npos ||
        (value.size() >= 3 && value.compare(value.size() - 3, 3, "/..") == 0))
        return "";
    return root + value;
}

File FS::open(const char *path, const char *mode, bool create) {
    std::string host = resolve(path, "open", 24);
    if (host.empty())
        return File();
    auto impl = std::make_shared<FileImpl>();
    impl->host_path = host;
    impl->path = path;
    struct stat status;
    bool exists = ::stat(host.c_str(), &status) == 0;
    if (exists && S_ISDIR(status.st_mode)) {
        impl->directory = true;
        if (DIR *directory = ::opendir(host.c_str())) {
            while (dirent *entry = ::readdir(directory)) {
                if (std::strcmp(entry->d_name, ".") && std::strcmp(entry->d_name, ".."))
                    impl->entries.push_back(entry->d_name);
            }
            ::closedir(directory);
        }
        std::sort(impl->entries.begin(), impl->entries.end());
        return File(impl, this);
    }
    std::string host_mode = mode ? mode : "r";
    if (!exists && host_mode[0] == 'r') {
        flapsim_log('E', "vfs_api.cpp", 105, "open", "%s does not exist, no permits for creation",
                    (mount_point + path).c_str());
        return File();
    }
    if (create) {
        for (size_t slash = root.size() + 1; (slash = host.find('/', slash)) != std::string::npos;
             slash++)
            ::mkdir(host.substr(0, slash).c_str(), 0755);
    }
    impl->file = std::fopen(host.c_str(), (host_mode + "b").c_str());
    if (!impl->file) {
        flapsim_log('E', "vfs_api.cpp", 105, "open", "%s does not exist, no permits for creation",
                    (mount_point + path).c_str());
        return File();
    }
    return File(impl, this);
}

bool FS::exists(const char *path) {
    std::string host = resolve(path, "exists", 113);
    struct stat status;
    return !host.empty() && ::stat(host.c_str(), &status) == 0;
}

bool FS::remove(const char *path) {
    std::string host = resolve(path, "remove", 168);
    return !host.empty() && ::unlink(host.c_str()) == 0;
}

bool FS::rename(const char *from, const char *to) {
    std::string source = resolve(from, "rename", 128), target = resolve(to, "rename", 128);
    return !source.empty() && !target.empty() && std::rename(source.c_str(), target.c_str()) == 0;
}

bool FS::mkdir(const char *path) {
    std::string host = resolve(path, "mkdir", 204);
    return !host.empty() && (::mkdir(host.c_str(), 0755) == 0 || errno == EEXIST);
}

bool FS::rmdir(const char *path) {
    std::string host = resolve(path, "rmdir", 204);
    return !host.empty() && ::rmdir(host.c_str()) == 0;
}

} // namespace fs

bool LittleFSFS::begin(bool format_on_fail, const char *base_path, uint8_t, const char *) {
    if (mounted)
        return true;
    root = flapsim::host::config().littlefs;
    struct stat status;
    if (::stat(root.c_str(), &status) != 0 || !S_ISDIR(status.st_mode)) {
        if (!format_on_fail || ::mkdir(root.c_str(), 0755) != 0) {
            flapsim_log('E', "LittleFS.cpp", 98, "begin", "Mounting LittleFS failed! Error: %d",
                        -84);
            return false;
        }
    }
    mount_point = base_path ? base_path : "/littlefs";
    mounted = true;
    return true;
}

bool LittleFSFS::format() {
    flapsim_log('E', "LittleFS.cpp", 123, "format", "Formatting LittleFS failed! Error: %d", -1);
    return false;
}

size_t LittleFSFS::usedBytes() {
    size_t total = 0;
    File directory = open("/");
    for (File file = directory.openNextFile(); file; file = directory.openNextFile())
        total += file.size();
    return total;
}
