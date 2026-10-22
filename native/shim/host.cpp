/* SPDX-License-Identifier: GPL-2.0-only */
#include "host.h"

#include "Arduino.h"
#include "vclock.h"

extern "C" {
#include "control.h"
#include "model.h"
}

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <csignal>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <exception>
#include <execinfo.h>
#include <fcntl.h>
#include <json.h>
#include <poll.h>
#include <stdexcept>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>
#include <vector>

extern char **environ;
void flapsim_freertos_enter_loop_task();

namespace flapsim::host {
namespace {

constexpr const char *kBootVariable = "FLAPSIM_NATIVE_BOOT";
constexpr int kExitInvalid = 2;
constexpr int kExitRuntime = 3;

Config settings;
unsigned boots = 1;
esp_reset_reason_t reason = ESP_RST_POWERON;
std::atomic<bool> clock_set{false};
int serial_fd = -1, input_fd = -1;
std::mutex input_mutex;
std::deque<uint8_t> input;
std::timed_mutex model_mutex;
char **saved_argv;
/* Prepared at startup so the reset path needs no allocation (it also runs in signal handlers). */
std::vector<char *> exec_environment;
char boot_slot[96];
char *alternate_stack;

[[noreturn]] void fail(const std::string &message) {
    std::fprintf(stderr, "flapsim-native: %s\n", message.c_str());
    std::exit(kExitInvalid);
}

json_object *member(json_object *object, const char *key, json_type type, bool required) {
    json_object *value = nullptr;
    if (!json_object_object_get_ex(object, key, &value) ||
        json_object_is_type(value, json_type_null)) {
        if (required)
            fail(std::string("native config: missing ") + key);
        return nullptr;
    }
    if (!json_object_is_type(value, type) &&
        !(type == json_type_double && json_object_is_type(value, json_type_int)))
        fail(std::string("native config: invalid ") + key);
    return value;
}

std::string path(json_object *object, const char *key, bool required) {
    json_object *value = member(object, key, json_type_string, required);
    if (!value)
        return "";
    std::string result = json_object_get_string(value);
    if (result.empty() || result[0] != '/' ||
        result.size() != size_t(json_object_get_string_len(value)))
        fail(std::string("native config: ") + key + " must be an absolute path without NUL");
    return result;
}

void load_config(const char *file) {
    json_object *root = json_object_from_file(file);
    if (!root || !json_object_is_type(root, json_type_object))
        fail(std::string("cannot read native config JSON: ") + file);
    static const char *known[] = {
        "schema_version", "model_config", "serial_log", "serial_input", "nvs",  "littlefs",
        "speed",          "random_seed",  "wifi",       "http_fd",      "clock"};
    json_object_object_foreach(root, key, unused) {
        (void)unused;
        if (std::none_of(std::begin(known), std::end(known),
                         [key](const char *name) { return !std::strcmp(name, key); }))
            fail(std::string("native config: unknown field ") + key);
    }
    if (json_object_get_int64(member(root, "schema_version", json_type_int, true)) != 1)
        fail("native config: schema_version must be 1");
    settings.model_config = path(root, "model_config", true);
    settings.serial_log = path(root, "serial_log", true);
    settings.serial_input = path(root, "serial_input", false);
    settings.nvs = path(root, "nvs", true);
    settings.littlefs = path(root, "littlefs", true);
    if (json_object *speed = member(root, "speed", json_type_double, false))
        settings.speed = json_object_get_double(speed);
    if (json_object *seed = member(root, "random_seed", json_type_int, false)) {
        int64_t value = json_object_get_int64(seed);
        if (value < 0 || value > 0xffffffffLL)
            fail("native config: random_seed must be a 32-bit unsigned integer");
        settings.random_seed = uint32_t(value);
    }
    if (json_object *http = member(root, "http_fd", json_type_int, false)) {
        int64_t fd = json_object_get_int64(http);
        if (fd < 3 || fd > 1023 || fcntl(int(fd), F_GETFD) < 0)
            fail("native config: http_fd must be an open inherited descriptor");
        settings.http_fd = int(fd);
    }
    if (json_object *clock = member(root, "clock", json_type_object, false)) {
        json_object_object_foreach(clock, key, unused) {
            (void)unused;
            if (std::strcmp(key, "mode") && std::strcmp(key, "start") && std::strcmp(key, "sync"))
                fail(std::string("native config: unknown clock field ") + key);
        }
        if (json_object *mode = member(clock, "mode", json_type_string, false)) {
            std::string value = json_object_get_string(mode);
            if (value != "host" && value != "fixed")
                fail("native config: clock.mode must be host or fixed");
            settings.clock.fixed = value == "fixed";
        }
        if (json_object *start = member(clock, "start", json_type_int, settings.clock.fixed)) {
            settings.clock.start = json_object_get_int64(start);
            if (!settings.clock.fixed || settings.clock.start < 0 ||
                settings.clock.start > 4102444800LL)
                fail("native config: clock.start is a fixed-mode epoch second before 2100");
        }
        if (json_object *sync = member(clock, "sync", json_type_string, false)) {
            std::string value = json_object_get_string(sync);
            if (value != "station" && value != "boot")
                fail("native config: clock.sync must be station or boot");
            settings.clock.sync_at_boot = value == "boot";
        }
    }
    if (json_object *wifi = member(root, "wifi", json_type_object, false)) {
        json_object_object_foreach(wifi, key, unused) {
            (void)unused;
            if (std::strcmp(key, "station") && std::strcmp(key, "connect_delay_ms") &&
                std::strcmp(key, "ip"))
                fail(std::string("native config: unknown wifi field ") + key);
        }
        if (json_object *station = member(wifi, "station", json_type_string, false)) {
            std::string mode = json_object_get_string(station);
            if (mode != "connect" && mode != "fail")
                fail("native config: wifi.station must be connect or fail");
            settings.wifi.station_connects = mode == "connect";
        }
        if (json_object *delay = member(wifi, "connect_delay_ms", json_type_int, false)) {
            settings.wifi.connect_delay_ms = json_object_get_int64(delay);
            if (settings.wifi.connect_delay_ms < 0 || settings.wifi.connect_delay_ms > 600000)
                fail("native config: wifi.connect_delay_ms must be in [0, 600000]");
        }
        if (json_object *ip = member(wifi, "ip", json_type_string, false)) {
            IPAddress parsed;
            if (!parsed.fromString(json_object_get_string(ip)))
                fail("native config: wifi.ip must be an IPv4 address");
            settings.wifi.ip = json_object_get_string(ip);
        }
    }
    json_object_put(root);
}

/* FLAPSIM_NATIVE_BOOT=<boot>:<reason>:<guest_ns>:<rtc set> is set only by an emulated reset; the
 * RTC keeps a synchronized wall clock across software resets, as on the device. */
int64_t load_boot_state() {
    const char *value = std::getenv(kBootVariable);
    if (!value)
        return 0;
    unsigned long long count = 0, code = 0, rtc = 0;
    long long guest = 0;
    char tail;
    if (std::sscanf(value, "%llu:%llu:%lld:%llu%c", &count, &code, &guest, &rtc, &tail) != 4 ||
        count < 2 || code > ESP_RST_SDIO || guest < 0 || rtc > 1)
        fail("invalid reset state in FLAPSIM_NATIVE_BOOT");
    boots = unsigned(count);
    reason = esp_reset_reason_t(code);
    clock_set = rtc == 1;
    return guest;
}

void prepare_exec(char **argv) {
    saved_argv = argv;
    size_t prefix = std::strlen(kBootVariable);
    for (char **entry = environ; *entry; entry++) {
        if (std::strncmp(*entry, kBootVariable, prefix) || (*entry)[prefix] != '=')
            exec_environment.push_back(*entry);
    }
    exec_environment.push_back(boot_slot);
    exec_environment.push_back(nullptr);
}

/* Async-signal-safe decimal formatting. */
char *append_number(char *out, unsigned long long value) {
    char digits[24];
    int count = 0;
    do {
        digits[count++] = char('0' + value % 10);
        value /= 10;
    } while (value);
    while (count)
        *out++ = digits[--count];
    return out;
}

void write_all(int fd, const void *data, size_t size) {
    const char *bytes = static_cast<const char *>(data);
    while (size) {
        ssize_t written = ::write(fd, bytes, size);
        if (written < 0 && errno == EINTR)
            continue;
        if (written <= 0)
            return;
        bytes += written;
        size -= size_t(written);
    }
}

void write_text(int fd, const char *text) { write_all(fd, text, std::strlen(text)); }

[[noreturn]] void reexec(esp_reset_reason_t next) {
    char *out = boot_slot;
    std::memcpy(out, kBootVariable, std::strlen(kBootVariable));
    out += std::strlen(kBootVariable);
    *out++ = '=';
    out = append_number(out, boots + 1);
    *out++ = ':';
    out = append_number(out, unsigned(next));
    *out++ = ':';
    out = append_number(out, (unsigned long long)vclock::now_ns());
    *out++ = ':';
    out = append_number(out, clock_set ? 1 : 0);
    *out = '\0';
    execve("/proc/self/exe", saved_argv, exec_environment.data());
    write_text(STDERR_FILENO, "flapsim-native: emulated reset failed: cannot re-execute\n");
    _exit(kExitRuntime);
}

/* Normal resets close the model so its last record is complete; the next boot resumes it. */
[[noreturn]] void reset(esp_reset_reason_t next, bool wait_for_model) {
    bool locked = wait_for_model ? (model_mutex.lock(), true)
                                 : model_mutex.try_lock_for(std::chrono::milliseconds(200));
    if (locked) {
        /* The QEMU adapter's reset handler: bring the model up to date, record the reset. */
        fs_model_tick(vclock::now_ns());
        fs_model_guest_reset();
        fs_model_close();
    }
    reexec(next);
}

const char *signal_exception(int signal) {
    switch (signal) {
    case SIGILL:
        return "IllegalInstruction";
    case SIGFPE:
        return "IntegerDivideByZero";
    default:
        return "LoadProhibited";
    }
}

void fatal_signal(int signal) {
    if (signal == SIGABRT) {
        write_text(serial_fd, "\r\nabort() was called at PC 0x00000000 on core 1\r\n");
    } else {
        write_text(serial_fd, "Guru Meditation Error: Core  1 panic'ed (");
        write_text(serial_fd, signal_exception(signal));
        write_text(serial_fd, "). Exception was unhandled.\r\n");
    }
    write_text(serial_fd, "\r\nRebooting...\r\n");
    write_text(STDERR_FILENO, "flapsim-native: fatal signal ");
    char number[24];
    *append_number(number, unsigned(signal)) = '\0';
    write_text(STDERR_FILENO, number);
    write_text(STDERR_FILENO, "; emulating a panic reset\n");
    void *frames[64];
    backtrace_symbols_fd(frames, backtrace(frames, 64), STDERR_FILENO);
    reexec(ESP_RST_PANIC);
}

void install_panic_handlers() {
    /* A reset re-executes from a handler, which keeps that signal blocked across execve. */
    sigset_t all;
    sigfillset(&all);
    sigprocmask(SIG_UNBLOCK, &all, nullptr);
    std::signal(SIGPIPE, SIG_IGN);
    alternate_stack = new char[1 << 16];
    stack_t stack{};
    stack.ss_sp = alternate_stack;
    stack.ss_size = 1 << 16;
    sigaltstack(&stack, nullptr);
    struct sigaction action {};
    action.sa_handler = fatal_signal;
    action.sa_flags = SA_ONSTACK | SA_NODEFER;
    sigemptyset(&action.sa_mask);
    for (int signal : {SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGABRT})
        sigaction(signal, &action, nullptr);
    std::set_terminate([] {
        const char *what = "terminate called";
        std::string detail;
        if (auto current = std::current_exception()) {
            try {
                std::rethrow_exception(current);
            } catch (const std::exception &error) {
                detail = std::string("uncaught exception: ") + error.what();
                what = detail.c_str();
            } catch (...) {
                what = "uncaught non-standard exception";
            }
        }
        panic(what);
    });
}

void open_serial() {
    serial_fd =
        ::open(settings.serial_log.c_str(), O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
    if (serial_fd < 0)
        fail("cannot open serial log " + settings.serial_log + ": " + std::strerror(errno));
    if (!settings.serial_input.empty()) {
        /* Read-write keeps a FIFO open without a writer, so it never reports end of file. */
        input_fd = ::open(settings.serial_input.c_str(), O_RDWR | O_NONBLOCK | O_CLOEXEC);
        if (input_fd < 0)
            fail("cannot open serial input " + settings.serial_input + ": " + std::strerror(errno));
    }
}

void boot_banner() {
    char line[160];
    std::snprintf(line, sizeof(line),
                  "\r\nrst:%s,boot:0x13 (SPI_FAST_FLASH_BOOT)\r\nflapsim native boot %u\r\n",
                  reason == ESP_RST_POWERON ? "0x1 (POWERON_RESET)" : "0xc (SW_CPU_RESET)", boots);
    serial_write(line, std::strlen(line));
}

void start_model(int64_t guest) {
    std::lock_guard<std::timed_mutex> lock(model_mutex);
    int status = boots == 1 ? fs_model_init(settings.model_config.c_str())
                            : fs_model_resume(settings.model_config.c_str(), guest);
    if (status != 0) {
        std::fprintf(stderr, "flapsim model failed: %s\n", fs_model_error());
        std::exit(1);
    }
    std::thread([] {
        /* The QEMU adapter's 100 ms snapshot timer, on the same virtual clock. */
        while (true) {
            vclock::sleep_ns(100000000);
            std::lock_guard<std::timed_mutex> tick(model_mutex);
            fs_model_tick(vclock::now_ns());
            check_model();
        }
    }).detach();
}

void start_control() {
    const char *value = std::getenv("FLAPSIM_CONTROL_FD");
    if (!value)
        return;
    char *end;
    errno = 0;
    long parsed = std::strtol(value, &end, 10);
    if (errno || end == value || *end || parsed < 3 || parsed > INT32_MAX)
        fail("invalid FLAPSIM_CONTROL_FD");
    int fd = int(parsed);
    /* pass_fds clears close-on-exec: the same private channel survives firmware resets. */
    if (::fcntl(fd, F_GETFD) < 0)
        fail("invalid control descriptor");
    std::thread([fd] {
        pollfd request{fd, POLLIN, 0};
        while (true) {
            int ready = ::poll(&request, 1, -1);
            if (ready < 0 && errno == EINTR)
                continue;
            if (ready <= 0 || !(request.revents & POLLIN))
                break;
            int status;
            {
                std::lock_guard<std::timed_mutex> owner(model_mutex);
                status = fs_control_serve_native(fd, vclock::now_ns());
                check_model();
                if (status == 3) {
                    int64_t guest = vclock::now_ns();
                    if (fs_model_power_checkpoint(guest) != 0)
                        _exit(kExitRuntime);
                    char *snapshot = fs_model_snapshot(guest);
                    if (!snapshot)
                        _exit(kExitRuntime);
                    json_object *reply = json_object_new_object();
                    json_object_object_add(reply, "state", json_tokener_parse(snapshot));
                    std::free(snapshot);
                    std::string resume = std::to_string(boots + 1) + ":1:" + std::to_string(guest) +
                                         ":" + (clock_set ? "1" : "0");
                    json_object_object_add(reply, "resume", json_object_new_string(resume.c_str()));
                    fs_model_close();
                    const char *data =
                        json_object_to_json_string_ext(reply, JSON_C_TO_STRING_PLAIN);
                    ssize_t sent = ::send(fd, data, std::strlen(data), MSG_DONTWAIT | MSG_NOSIGNAL);
                    _exit(sent == (ssize_t)std::strlen(data) ? 0 : kExitRuntime);
                }
            }
            if (!status)
                break;
            if (status == 2)
                reset(ESP_RST_SW, true);
        }
        /* Retain the inherited descriptor for re-exec even after peer loss.
         * Closing it here would leave a stale FD in the saved reset environment. */
    }).detach();
}

void fill_input() {
    uint8_t buffer[256];
    while (input_fd >= 0) {
        ssize_t count = ::read(input_fd, buffer, sizeof(buffer));
        if (count <= 0)
            return;
        input.insert(input.end(), buffer, buffer + count);
    }
}

} // namespace

const Config &config() { return settings; }
std::atomic<bool> &rtc_clock_set() { return clock_set; }
unsigned boot_count() { return boots; }
esp_reset_reason_t reset_reason() { return reason; }

void serial_write(const void *data, size_t size) { write_all(serial_fd, data, size); }

int serial_available() {
    std::lock_guard<std::mutex> lock(input_mutex);
    fill_input();
    return int(input.size());
}

int serial_read() {
    std::lock_guard<std::mutex> lock(input_mutex);
    fill_input();
    if (input.empty())
        return -1;
    int value = input.front();
    input.pop_front();
    return value;
}

int serial_peek() {
    std::lock_guard<std::mutex> lock(input_mutex);
    fill_input();
    return input.empty() ? -1 : input.front();
}

std::timed_mutex &model_lock() { return model_mutex; }

void check_model() {
    if (*fs_model_error()) {
        std::fprintf(stderr, "flapsim model failed: %s\n", fs_model_error());
        std::_Exit(1);
    }
}

void restart(esp_reset_reason_t next) { reset(next, true); }

void panic(const char *what) {
    std::fprintf(stderr, "flapsim-native: panic: %s\n", what);
    write_text(serial_fd,
               "\r\nabort() was called at PC 0x00000000 on core 1\r\n\r\nRebooting...\r\n");
    reset(ESP_RST_PANIC, false);
}

} // namespace flapsim::host

void flapsim_log(char level, const char *file, unsigned line, const char *function,
                 const char *format, ...) {
    const char *base = std::strrchr(file, '/');
    char prefix[160];
    int length =
        std::snprintf(prefix, sizeof(prefix), "[%6u][%c][%s:%u] %s(): ", unsigned(millis()), level,
                      base ? base + 1 : file, line, function);
    va_list args;
    va_start(args, format);
    char message[512];
    int body = std::vsnprintf(message, sizeof(message), format, args);
    va_end(args);
    std::string record(prefix, size_t(std::max(0, std::min<int>(length, sizeof(prefix) - 1))));
    record.append(message, size_t(std::max(0, std::min<int>(body, sizeof(message) - 1))));
    record += "\r\n";
    flapsim::host::serial_write(record.data(), record.size());
}

namespace flapsim::host {

void start(int argc, char **argv) {
    if (argc != 3 || std::strcmp(argv[1], "--config"))
        fail("usage: flapsim-native --config <native-config.json> | --build-info");
    load_config(argv[2]);
    int64_t guest = load_boot_state();
    try {
        vclock::init(guest, guest, settings.speed);
    } catch (const std::invalid_argument &error) {
        fail(std::string("native config: ") + error.what());
    }
    prepare_exec(argv);
    open_serial();
    install_panic_handlers();
    boot_banner();
    start_model(guest);
    start_control();
    flapsim_freertos_enter_loop_task();
}

void configure_for_tests(const Config &config) {
    settings = config;
    vclock::init(0, 0, settings.speed);
    open_serial();
}

} // namespace flapsim::host
