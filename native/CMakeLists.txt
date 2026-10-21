# SPDX-License-Identifier: GPL-2.0-only
# Configured by native/build.py, which fetches and verifies every input first.
cmake_minimum_required(VERSION 3.20)
project(flapsim_native LANGUAGES C CXX)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS ON)

foreach(required FIRMWARE_SRC SKETCH_SOURCE GENERATED_DIR LIBRARY_INCLUDES)
    if(NOT DEFINED ${required})
        message(FATAL_ERROR "${required} must be set by native/build.py")
    endif()
endforeach()

option(FLAPSIM_SANITIZE "Build with address and undefined-behaviour sanitizers" OFF)
if(FLAPSIM_SANITIZE)
    add_compile_options(-fsanitize=address,undefined -fno-omit-frame-pointer)
    add_link_options(-fsanitize=address,undefined)
    # Sanitizer instrumentation makes GCC report false maybe-uninitialized in inlined
    # ArduinoJson templates; every other warning stays an error.
    add_compile_options(-Wno-maybe-uninitialized)
endif()

find_package(PkgConfig REQUIRED)
pkg_check_modules(JSONC REQUIRED IMPORTED_TARGET json-c)
find_package(Threads REQUIRED)

set(ROOT "${CMAKE_CURRENT_SOURCE_DIR}/..")

# Board and Arduino identity shared by every translation unit, so library configuration
# (for example ArduinoJson's Arduino String support) agrees between shims and firmware.
add_library(flapsim_platform INTERFACE)
target_compile_definitions(flapsim_platform INTERFACE
    ARDUINO=10812 ARDUINO_ARCH_ESP32=1 ESP32=1 CONFIG_IDF_TARGET_ESP32=1 FLAPSIM_NATIVE=1
    ARDUINOJSON_ENABLE_PROGMEM=0)
target_include_directories(flapsim_platform INTERFACE "${CMAKE_CURRENT_SOURCE_DIR}/shim")
# Third-party headers are system headers: their warnings never fail the shim's -Werror build.
target_include_directories(flapsim_platform SYSTEM INTERFACE ${LIBRARY_INCLUDES})

add_library(flapsim_model STATIC
    "${ROOT}/model/bus.c" "${ROOT}/model/config.c" "${ROOT}/model/control.c"
    "${ROOT}/model/drum.c" "${ROOT}/model/io.c"
    "${ROOT}/model/pcf8575.c" "${ROOT}/model/resume.c" "${ROOT}/model/stepper.c")
target_include_directories(flapsim_model PUBLIC "${ROOT}/model")
target_compile_options(flapsim_model PRIVATE -Wall -Wextra -Werror)
target_link_libraries(flapsim_model PUBLIC PkgConfig::JSONC)

file(GLOB SHIM_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/shim/*.cpp")
add_library(flapsim_shim STATIC ${SHIM_SOURCES})
target_compile_options(flapsim_shim PRIVATE -Wall -Wextra -Werror)
target_link_libraries(flapsim_shim PUBLIC flapsim_platform flapsim_model Threads::Threads)

# Firmware and third-party sources compile unmodified and with their own warning levels.
file(GLOB FIRMWARE_SOURCES CONFIGURE_DEPENDS "${FIRMWARE_SRC}/src/*.cpp")
add_executable(flapsim-native main.cpp ${FIRMWARE_SOURCES} "${SKETCH_SOURCE}" ${LIBRARY_SOURCES})
target_include_directories(flapsim-native PRIVATE "${FIRMWARE_SRC}/src" "${GENERATED_DIR}")
# PlatformIO build_flags apply to the sketch, firmware and libraries alike, as on the device.
target_compile_options(flapsim-native PRIVATE -include "${GENERATED_DIR}/flapsim_firmware.h")
target_link_libraries(flapsim-native PRIVATE flapsim_shim)
target_link_options(flapsim-native PRIVATE -rdynamic)
install(TARGETS flapsim-native RUNTIME DESTINATION bin)

# Shim behaviour tests: no firmware, the same shim library and hardware model.
add_executable(flapsim-shim-tests EXCLUDE_FROM_ALL tests/shim_tests.cpp)
target_compile_options(flapsim-shim-tests PRIVATE -Wall -Wextra -Werror)
target_link_libraries(flapsim-shim-tests PRIVATE flapsim_shim)
