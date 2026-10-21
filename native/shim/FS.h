/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include "Stream.h"

#include <cstdio>
#include <memory>
#include <string>
#include <vector>

/* arduino-esp32 fs::FS over a host directory that stands in for the mounted partition. */
namespace fs {

enum SeekMode { SeekSet = 0, SeekCur = 1, SeekEnd = 2 };

struct FileImpl {
    std::string host_path, path;
    bool directory = false;
    std::FILE *file = nullptr;
    std::vector<std::string> entries;
    size_t next_entry = 0;
    ~FileImpl();
};

class FS;

class File : public Stream {
  public:
    File() = default;
    explicit File(std::shared_ptr<FileImpl> impl, FS *owner)
        : impl(std::move(impl)), owner(owner) {}
    using Print::write;
    size_t write(uint8_t value) override { return write(&value, 1); }
    size_t write(const uint8_t *buffer, size_t size) override;
    int available() override;
    int read() override;
    size_t read(uint8_t *buffer, size_t size);
    size_t readBytes(char *buffer, size_t length) override {
        return read(reinterpret_cast<uint8_t *>(buffer), length);
    }
    int peek() override;
    void flush() override;
    bool seek(uint32_t position, SeekMode mode = SeekSet);
    size_t position() const;
    size_t size() const;
    void close() { impl.reset(); }
    explicit operator bool() const { return impl != nullptr; }
    const char *path() const { return impl ? impl->path.c_str() : nullptr; }
    const char *name() const;
    bool isDirectory() const { return impl && impl->directory; }
    File openNextFile(const char *mode = "r");
    void rewindDirectory() {
        if (impl)
            impl->next_entry = 0;
    }

  private:
    std::shared_ptr<FileImpl> impl;
    FS *owner = nullptr;
};

class FS {
  public:
    virtual ~FS() = default;
    File open(const char *path, const char *mode = "r", bool create = false);
    File open(const String &path, const char *mode = "r", bool create = false) {
        return open(path.c_str(), mode, create);
    }
    bool exists(const char *path);
    bool exists(const String &path) { return exists(path.c_str()); }
    bool remove(const char *path);
    bool remove(const String &path) { return remove(path.c_str()); }
    bool rename(const char *from, const char *to);
    bool mkdir(const char *path);
    bool rmdir(const char *path);
    /* Host path of a mounted absolute path, or empty when unmounted or escaping the mount. */
    std::string resolve(const char *path, const char *function, unsigned line);

  protected:
    std::string root, mount_point;
    bool mounted = false;
};

} // namespace fs

using fs::File;
using fs::FS;
using fs::SeekCur;
using fs::SeekEnd;
using fs::SeekMode;
using fs::SeekSet;
