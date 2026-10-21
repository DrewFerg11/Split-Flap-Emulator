/* SPDX-License-Identifier: GPL-2.0-only */
/* Process entry: the Arduino loop task on the main thread, as arduino-esp32's app_main runs it. */
#include "flapsim_build_info.h"
#include "host.h"

#include <cstdio>
#include <cstring>

void setup();
void loop();

int main(int argc, char **argv) {
    if (argc == 2 && !std::strcmp(argv[1], "--build-info")) {
        std::puts(FLAPSIM_NATIVE_BUILD_INFO);
        return 0;
    }
    flapsim::host::start(argc, argv);
    setup();
    while (true)
        loop();
}
