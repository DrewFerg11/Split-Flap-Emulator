/* SPDX-License-Identifier: GPL-2.0-only */
/* Behaviour checks for the native shims against the arduino-esp32 2.0.17 / ESP-IDF 4.4 and
 * ESPAsyncWebServer 3.6 semantics the firmware relies on. Usage: flapsim-shim-tests <tmpdir> */
#include "AsyncJson.h"
#include "LittleFS.h"
#include "Preferences.h"
#include "WiFi.h"
#include "Wire.h"
#include "host.h"
#include "vclock.h"

extern "C" {
#include "model.h"
}

#include <arpa/inet.h>
#include <cstdio>
#include <fstream>
#include <functional>
#include <netinet/in.h>
#include <sstream>
#include <string>
#include <sys/socket.h>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>
#include <vector>

namespace {

int failures = 0;
std::string directory;

#define CHECK(condition)                                                                           \
    do {                                                                                           \
        if (!(condition)) {                                                                        \
            std::fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #condition);     \
            failures++;                                                                            \
        }                                                                                          \
    } while (0)

std::string read_file(const std::string &path) {
    std::ifstream file(path, std::ios::binary);
    std::stringstream text;
    text << file.rdbuf();
    return text.str();
}

void write_file(const std::string &path, const std::string &text) {
    std::ofstream(path, std::ios::binary) << text;
}

std::string serial_since(size_t &cursor) {
    std::string text = read_file(flapsim::host::config().serial_log);
    std::string fresh = text.substr(cursor);
    cursor = text.size();
    return fresh;
}

class Capture : public Print {
  public:
    using Print::write;
    size_t write(uint8_t value) override {
        text += char(value);
        return 1;
    }
    std::string text;
};

void strings() {
    CHECK(String(15.0f) == "15.00");
    CHECK(String(5.0f, 0) == " 5");
    CHECK(String(-0.5f) == "-0.50");
    CHECK(String(1.999, 2) == "2.00");
    CHECK(String(255, HEX) == "ff");
    CHECK(String(-42) == "-42");
    CHECK(String("hello").substring(1, 3) == "el");
    CHECK(String("hello").substring(3, 1) == "el");
    CHECK(String("hello").substring(5) == "");
    CHECK(String("hello").substring(2, 99) == "llo");
    String replaced("a%20b%20c");
    replaced.replace("%20", " ");
    CHECK(replaced == "a b c");
    CHECK(String("a,b,c").indexOf(',') == 1 && String("a,b,c").lastIndexOf(',') == 3);
    CHECK(String("abc").indexOf('c', 3) == -1);
    CHECK(String(" 42x").toInt() == 42);
    CHECK(String("Application/JSON").equalsIgnoreCase("application/json"));
    CHECK(String("index.html.gz").endsWith(".gz") && String("/settings").startsWith("/set"));
    String removed("1,2,");
    removed.remove(removed.length() - 1);
    CHECK(removed == "1,2");
    String sum = "Network " + String("x") + " " + 7 + '!';
    CHECK(sum == "Network x 7!");
    Capture out;
    out.print(1.999);
    out.print(' ');
    out.println(uint8_t(32));
    out.printf("[%d]0x%02X", 3, 0x21);
    CHECK(out.text == "2.00 32\r\n[3]0x21");
}

void preferences() {
    size_t cursor = 0;
    serial_since(cursor);
    Preferences settings;
    CHECK(!settings.begin("config", true));
    std::string log = serial_since(cursor);
    CHECK(log.find("][E][Preferences.cpp:50] begin(): nvs_open failed: NOT_FOUND\r\n") !=
          std::string::npos);
    CHECK(settings.getString("name", "fallback") == "fallback");
    CHECK(serial_since(cursor).empty());

    CHECK(settings.begin("config"));
    CHECK(!settings.begin("config")); /* already started: silently refused, as on the device */
    CHECK(settings.putString("ssid", "home") == 4);
    CHECK(settings.putInt("mode", 3) == 4);
    CHECK(settings.putFloat("maxVel", 12.5f) == sizeof(float));
    CHECK(settings.getString("missing", "d") == "d");
    log = serial_since(cursor);
    CHECK(
        log.find("[E][Preferences.cpp:483] getString(): nvs_get_str len fail: missing NOT_FOUND") !=
        std::string::npos);
    CHECK(settings.getInt("ssid", -1) == -1); /* typed keys */
    CHECK(settings.putInt("waytoolongkeyname", 1) == 0);
    CHECK(serial_since(cursor).find("putInt(): nvs_set_i32 fail: waytoolongkeyname KEY_TOO_LONG") !=
          std::string::npos);
    settings.end();
    CHECK(settings.getInt("mode", -1) == -1); /* not started: default, no log */

    CHECK(settings.begin("config", true));
    CHECK(settings.getString("ssid") == "home" && settings.getInt("mode") == 3 &&
          settings.getFloat("maxVel") == 12.5f);
    CHECK(settings.putInt("mode", 9) == 0 && settings.getInt("mode") == 3); /* read-only handle */
    settings.end();

    std::string stored = read_file(flapsim::host::config().nvs);
    CHECK(stored.find("\"ssid\"") != std::string::npos &&
          stored.find("\"home\"") != std::string::npos);

    nvs_handle_t handle;
    CHECK(flapsim::nvs::open("config", false, &handle) == ESP_OK);
    flapsim::nvs::close(handle);
    std::string bytes;
    CHECK(flapsim::nvs::get(handle, "ssid", NvsType::Str, &bytes) == ESP_ERR_NVS_INVALID_HANDLE);
    CHECK(std::string(flapsim::nvs::error_name(ESP_ERR_NVS_INVALID_HANDLE)) == "INVALID_HANDLE");

    CHECK(settings.begin("config"));
    CHECK(settings.clear());
    CHECK(!settings.isKey("ssid"));
    settings.end();
}

void clock() {
    using namespace flapsim;
    int64_t before = vclock::now_ns();
    vclock::set_speed(50.0);
    int64_t start = vclock::now_ns();
    CHECK(start >= before);
    delay(500); /* 10 ms of host time at 50x */
    int64_t elapsed = vclock::now_ns() - start;
    CHECK(elapsed >= 500000000 && elapsed < 2000000000);
    vclock::set_speed(1.0);
    CHECK(vclock::now_ns() >= start + elapsed);
    bool rejected = false;
    try {
        vclock::set_speed(100.0);
    } catch (const std::invalid_argument &) {
        rejected = true;
    }
    CHECK(rejected);
    vclock::set_speed(20.0);
    SemaphoreHandle_t done = xSemaphoreCreateCounting(2, 0);
    unsigned long waited = millis();
    CHECK(xSemaphoreTake(done, pdMS_TO_TICKS(100)) == pdFALSE);
    CHECK(millis() - waited >= 100);
    static int ran = 0;
    xTaskCreatePinnedToCore(
        [](void *semaphore) {
            ran++;
            xSemaphoreGive(static_cast<SemaphoreHandle_t>(semaphore));
            vTaskDelete(NULL);
        },
        "test", 4096, done, 1, nullptr, 0);
    CHECK(xSemaphoreTake(done, portMAX_DELAY) == pdTRUE && ran == 1);
    vSemaphoreDelete(done);
    vclock::set_speed(1.0);
}

void filesystem() {
    size_t cursor = 0;
    serial_since(cursor);
    std::string root = flapsim::host::config().littlefs;
    mkdir(root.c_str(), 0755);
    write_file(root + "/timezones.json", "{\"UTC\":\"UTC0\"}");
    write_file(root + "/index.html.gz", "GZ");
    CHECK(!LittleFS.open("/timezones.json"));
    CHECK(serial_since(cursor).find("[E][vfs_api.cpp:24] open(): File system is not mounted") !=
          std::string::npos);
    CHECK(LittleFS.begin());
    File file = LittleFS.open("/timezones.json", "r");
    CHECK(file && file.size() == 14 && String(file.name()) == "timezones.json");
    char buffer[32] = {};
    CHECK(file.readBytes(buffer, file.size()) == 14 && std::string(buffer) == "{\"UTC\":\"UTC0\"}");
    CHECK(!LittleFS.open("/missing.json", "r"));
    CHECK(serial_since(cursor).find(
              "[E][vfs_api.cpp:105] open(): /littlefs/missing.json does not exist, "
              "no permits for creation") != std::string::npos);
    std::vector<std::string> names;
    File directory = LittleFS.open("/");
    CHECK(directory.isDirectory());
    for (File entry = directory.openNextFile(); entry; entry = directory.openNextFile())
        names.push_back(entry.name());
    CHECK((names == std::vector<std::string>{"index.html.gz", "timezones.json"}));
    CHECK(!LittleFS.exists("/../escape"));
}

AsyncWebServerRequest::Response request(AsyncWebServer &server, WebRequestMethodComposite method,
                                        const char *url, const char *type = "",
                                        const char *body = "") {
    AsyncWebServerRequest call(method, url, type, body);
    server.dispatch(call);
    return call.result();
}

void webserver() {
    AsyncWebServer server(80);
    std::vector<std::string> calls;
    server.on("/", HTTP_GET, [](AsyncWebServerRequest *r) { r->redirect("/index.html"); });
    server.serveStatic("/index.html", LittleFS, "/index.html", "max-age=600");
    server.on("/settings", HTTP_GET,
              [](AsyncWebServerRequest *r) { r->send(200, "application/json", "{}"); });
    server.addHandler(new AsyncCallbackJsonWebHandler(
        "/settings", [&](AsyncWebServerRequest *r, JsonVariant &json) {
            calls.push_back(json["mode"].as<String>().c_str());
            if (r->method() != HTTP_POST)
                return r->send(405);
            r->send(200, "application/json", "{\"type\":\"success\"}");
        }));
    server.onNotFound([](AsyncWebServerRequest *r) { r->send(404); });

    auto redirect = request(server, HTTP_GET, "/");
    CHECK(redirect.code == 302 && redirect.headers.at(0).second == "/index.html");
    auto page = request(server, HTTP_GET, "/index.html");
    CHECK(page.code == 200 && page.body == "GZ" && page.content_type == "text/html");
    bool gzip = false;
    for (auto &[name, value] : page.headers)
        gzip |= name == "Content-Encoding" && value == "gzip";
    CHECK(gzip);
    CHECK(request(server, HTTP_GET, "/settings").body == "{}");
    CHECK(request(server, HTTP_POST, "/settings", "application/json", "{\"mode\":\"x\"}").code ==
          200);
    CHECK(calls.size() == 1 && calls[0] == "x");
    CHECK(request(server, HTTP_POST, "/settings", "text/plain", "{}").code == 404);
    CHECK(request(server, HTTP_POST, "/settings", "application/json", "not json").code == 400);
    CHECK(request(server, HTTP_POST, "/settings", "application/json", "").code == 400);
    std::string large(20000, ' ');
    CHECK(request(server, HTTP_POST, "/settings", "application/json", large.c_str()).code == 413);
    CHECK(request(server, HTTP_PUT, "/settings/nested", "application/json", "{}").code == 405);
    CHECK(request(server, HTTP_GET, "/nowhere").code == 404);
}

void wire() {
    std::string config = directory + "/model.json";
    write_file(config,
               "{\"schema_version\":1,\"chip\":\"esp32\",\"drum\":{\"steps_per_rotation\":2048,"
               "\"flaps\":\" ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\",\"magnet_position\":730,"
               "\"sensor_window\":40},\"modules\":[{\"bus\":1,\"addr\":32,\"angle\":735}],"
               "\"outputs\":{\"state\":\"" +
                   directory + "/state.json\",\"activity\":\"" + directory +
                   "/activity.json\",\"timeline\":\"" + directory +
                   "/timeline.jsonl\",\"trace\":null}}");
    CHECK(fs_model_init(config.c_str()) == 0);
    CHECK(Wire1.endTransmission() == 4); /* not begun */
    CHECK(Wire1.begin(33, 32));
    Wire1.beginTransmission(uint8_t(0x21));
    CHECK(Wire1.endTransmission() == 2); /* absent module NACKs its address */
    Wire1.beginTransmission(uint8_t(0x20));
    Wire1.write(0xE7);
    Wire1.write(0xFF);
    CHECK(Wire1.endTransmission() == 0);
    CHECK(Wire1.requestFrom(uint8_t(0x20), uint8_t(2)) == 2 && Wire1.available() == 2);
    uint16_t input = uint16_t(Wire1.read());
    input |= uint16_t(Wire1.read() << 8);
    CHECK((input & 0x8000) != 0); /* angle 735 is inside the magnet window */
    CHECK((input & 0x7fff) == (0xFFE7 & 0x7fff));
    Wire.begin(21, 22);
    Wire.beginTransmission(uint8_t(0x20));
    CHECK(Wire.endTransmission() == 2); /* bus 0 has no modules here */
    fs_model_close();
    CHECK(read_file(directory + "/timeline.jsonl").find("\"anomalies\":0") != std::string::npos);
}

int listening_socket(uint16_t *port) {
    int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    socklen_t length = sizeof(address);
    ::bind(fd, reinterpret_cast<sockaddr *>(&address), length);
    ::listen(fd, 8);
    ::getsockname(fd, reinterpret_cast<sockaddr *>(&address), &length);
    *port = ntohs(address.sin_port);
    return fd;
}

/* One raw HTTP exchange; the request may be sent in pieces to exercise incremental parsing. */
std::string exchange(uint16_t port, const std::vector<std::string> &pieces) {
    int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(port);
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (::connect(fd, reinterpret_cast<sockaddr *>(&address), sizeof(address)) != 0)
        return "";
    for (const auto &piece : pieces) {
        ::send(fd, piece.data(), piece.size(), 0);
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    std::string response;
    char buffer[4096];
    for (ssize_t count; (count = ::recv(fd, buffer, sizeof(buffer), 0)) > 0;)
        response.append(buffer, size_t(count));
    ::close(fd);
    return response;
}

uint16_t http_port;

void http_transport() {
    static AsyncWebServer server(80);
    server.on("/echo", HTTP_POST | HTTP_GET, [](AsyncWebServerRequest *r) {
        r->send(200, "text/plain", r->url() + "|" + r->contentType() + "|" + r->body());
    });
    server.begin();
    std::string reply =
        exchange(http_port, {"POST /echo?x=1 HTTP/1.1\r\nContent-Type: application/json; "
                             "charset=utf-8\r\nContent-Length: 5\r\n\r\n{\"",
                             "a\"}"});
    CHECK(reply.find("HTTP/1.1 200 OK\r\n") == 0);
    CHECK(reply.find(
              "Content-Length: 28\r\nConnection: close\r\n\r\n/echo|application/json|{\"a\"}") !=
          std::string::npos);
    CHECK(exchange(http_port, {"GET /%65cho HTTP/1.1\r\n\r\n"}).find("/echo||") !=
          std::string::npos);
    CHECK(exchange(http_port, {"GET /missing HTTP/1.1\r\n\r\n"}).find("HTTP/1.1 404 Not Found") ==
          0);
    CHECK(exchange(http_port, {"POST /echo HTTP/1.1\r\nContent-Length: 99999999\r\n\r\n"})
              .find("HTTP/1.1 413") == 0);
    std::string oversized =
        "GET /echo HTTP/1.1\r\nX-Pad: " + std::string(17 * 1024, 'a') + "\r\n\r\n";
    CHECK(exchange(http_port, {oversized}).find("HTTP/1.1 413") == 0);
    CHECK(exchange(http_port, {"POST /echo HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n"
                               "5\r\nhello\r\n0\r\n\r\n"})
              .find("HTTP/1.1 501") == 0);
    CHECK(exchange(http_port, {"garbage\r\n\r\n"}).find("HTTP/1.1 400") == 0);
    CHECK(exchange(http_port, {"BREW /echo HTTP/1.1\r\n\r\n"}).find("HTTP/1.1 501") == 0);
}

void sockets() {
    uint16_t port;
    int listener = listening_socket(&port);
    WiFiClient client;
    IPAddress loopback(127, 0, 0, 1);
    CHECK(!client.connect(loopback, port)); /* no station association yet */
    WiFi.mode(WIFI_STA);
    WiFi.begin("lab", "secret");
    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < 5000)
        delay(10);
    CHECK(WiFi.status() == WL_CONNECTED && WiFi.localIP().toString() == "127.0.0.1");
    CHECK(client.connect("localhost", port));
    int peer = ::accept(listener, nullptr, nullptr);
    CHECK(client.write(reinterpret_cast<const uint8_t *>("ping"), 4) == 4);
    char buffer[8] = {};
    CHECK(::recv(peer, buffer, sizeof(buffer), 0) == 4 && std::string(buffer) == "ping");
    ::send(peer, "pong", 4, 0);
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    CHECK(client.available() == 4 && client.peek() == 'p' && client.read() == 'p');
    ::close(peer);
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    CHECK(client.connected()); /* unread data keeps it connected, as on the device */
    uint8_t rest[3];
    CHECK(client.read(rest, 3) == 3);
    CHECK(!client.connected());
    WiFi.disconnect(true);
    ::close(listener);
}

} // namespace

int main(int argc, char **argv) {
    if (argc != 2) {
        std::fprintf(stderr, "usage: %s <empty temporary directory>\n", argv[0]);
        return 2;
    }
    directory = argv[1];
    flapsim::host::Config config;
    config.serial_log = directory + "/serial.log";
    config.nvs = directory + "/nvs.json";
    config.littlefs = directory + "/littlefs";
    config.http_fd = listening_socket(&http_port);
    flapsim::host::configure_for_tests(config);
    for (auto test :
         {strings, preferences, clock, filesystem, webserver, http_transport, sockets, wire})
        test();
    std::printf("%s (%d failure%s)\n", failures ? "FAIL" : "PASS", failures,
                failures == 1 ? "" : "s");
    return failures ? 1 : 0;
}
