/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include "FS.h"

class LittleFSFS : public fs::FS {
  public:
    bool begin(bool format_on_fail = false, const char *base_path = "/littlefs",
               uint8_t max_open_files = 10, const char *partition_label = "spiffs");
    void end() { mounted = false; }
    bool format();
    size_t totalBytes() { return 0xf0000; }
    size_t usedBytes();
};

extern LittleFSFS LittleFS;
