/* SPDX-License-Identifier: GPL-2.0-only */
/* HTTP/1.1 transport for the AsyncWebServer adapter. One server thread accepts and parses
 * connections and runs every handler, like the device's async_tcp task. Each response closes
 * its connection. The listening socket is inherited from the runner (port 80 of the device), so it
 * stays open across emulated resets and queues requests until the next boot's begin(). */
#include "ESPAsyncWebServer.h"
#include "host.h"

#include <atomic>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <map>
#include <poll.h>
#include <string>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>
#include <vector>

namespace {

constexpr size_t kMaxHeader = 16 * 1024;
constexpr size_t kMaxBody = 1024 * 1024;
constexpr auto kIdle = std::chrono::seconds(30);

struct Connection {
    int fd;
    std::string data;
    std::chrono::steady_clock::time_point last;
};

struct Parsed {
    bool complete = false, invalid = false, too_large = false, unsupported = false;
    WebRequestMethodComposite method = 0;
    String url, content_type, body;
};

const char *reason(int code) {
    static const std::map<int, const char *> reasons = {{200, "OK"},
                                                        {204, "No Content"},
                                                        {301, "Moved Permanently"},
                                                        {302, "Found"},
                                                        {304, "Not Modified"},
                                                        {400, "Bad Request"},
                                                        {404, "Not Found"},
                                                        {405, "Method Not Allowed"},
                                                        {413, "Payload Too Large"},
                                                        {500, "Internal Server Error"},
                                                        {501, "Not Implemented"}};
    auto found = reasons.find(code);
    return found == reasons.end() ? "Unknown" : found->second;
}

WebRequestMethodComposite method_of(const std::string &name) {
    static const std::map<std::string, WebRequestMethodComposite> methods = {
        {"GET", HTTP_GET},     {"POST", HTTP_POST}, {"DELETE", HTTP_DELETE},  {"PUT", HTTP_PUT},
        {"PATCH", HTTP_PATCH}, {"HEAD", HTTP_HEAD}, {"OPTIONS", HTTP_OPTIONS}};
    auto found = methods.find(name);
    return found == methods.end() ? 0 : found->second;
}

int hex_digit(char c) {
    if (std::isdigit(static_cast<unsigned char>(c)))
        return c - '0';
    c = char(std::tolower(static_cast<unsigned char>(c)));
    return c >= 'a' && c <= 'f' ? c - 'a' + 10 : -1;
}

/* ESPAsyncWebServer exposes the percent-decoded path without its query string. */
String decode_path(const std::string &target) {
    std::string path = target.substr(0, target.find('?')), out;
    for (size_t i = 0; i < path.size(); i++) {
        int high, low;
        if (path[i] == '%' && i + 2 < path.size() && (high = hex_digit(path[i + 1])) >= 0 &&
            (low = hex_digit(path[i + 2])) >= 0) {
            out += char(high * 16 + low);
            i += 2;
        } else {
            out += path[i];
        }
    }
    return String(out.c_str());
}

Parsed parse(const std::string &data) {
    Parsed result;
    size_t end = data.find("\r\n\r\n");
    /* The limit holds whether or not the terminator arrived in the same read. */
    if (end == std::string::npos || end > kMaxHeader) {
        result.too_large = end != std::string::npos || data.size() > kMaxHeader;
        return result;
    }
    size_t line_end = data.find("\r\n");
    std::string request_line = data.substr(0, line_end);
    size_t first = request_line.find(' '), second = request_line.rfind(' ');
    if (first == std::string::npos || second == first ||
        request_line.compare(second + 1, 5, "HTTP/") != 0) {
        result.invalid = result.complete = true;
        return result;
    }
    result.method = method_of(request_line.substr(0, first));
    result.url = decode_path(request_line.substr(first + 1, second - first - 1));
    size_t length = 0;
    for (size_t at = line_end + 2; at < end;) {
        size_t next = data.find("\r\n", at);
        std::string header = data.substr(at, next - at);
        at = next + 2;
        size_t colon = header.find(':');
        if (colon == std::string::npos)
            continue;
        std::string name = header.substr(0, colon), value = header.substr(colon + 1);
        value.erase(0, value.find_first_not_of(" \t"));
        for (char &c : name)
            c = char(std::tolower(static_cast<unsigned char>(c)));
        if (name == "content-type") {
            result.content_type = value.substr(0, value.find(';')).c_str();
            result.content_type.trim();
        } else if (name == "content-length") {
            char *tail;
            unsigned long long parsed = std::strtoull(value.c_str(), &tail, 10);
            if (*tail || parsed > kMaxBody) {
                result.too_large = parsed > kMaxBody;
                result.invalid = !result.too_large;
                result.complete = true;
                return result;
            }
            length = size_t(parsed);
        } else if (name == "transfer-encoding") {
            /* Chunked request bodies are not decoded; refuse them rather than drop the body. */
            result.unsupported = result.complete = true;
            return result;
        }
    }
    if (data.size() - end - 4 < length)
        return result;
    result.body = String(data.data() + end + 4, unsigned(length));
    result.complete = true;
    return result;
}

void send_all(int fd, const std::string &bytes) {
    size_t sent = 0;
    while (sent < bytes.size()) {
        ssize_t count = ::send(fd, bytes.data() + sent, bytes.size() - sent, MSG_NOSIGNAL);
        if (count > 0) {
            sent += size_t(count);
            continue;
        }
        if (count < 0 && (errno == EAGAIN || errno == EINTR)) {
            pollfd entry{fd, POLLOUT, 0};
            if (::poll(&entry, 1, 5000) > 0)
                continue;
        }
        return;
    }
}

/* A HEAD response carries the GET headers, Content-Length included, but no body. */
std::string serialize(const AsyncWebServerRequest::Response &response, bool head) {
    std::string out =
        "HTTP/1.1 " + std::to_string(response.code) + " " + reason(response.code) + "\r\n";
    if (!response.content_type.isEmpty())
        out += std::string("Content-Type: ") + response.content_type.c_str() + "\r\n";
    for (const auto &[name, value] : response.headers)
        out += std::string(name.c_str()) + ": " + value.c_str() + "\r\n";
    out +=
        "Content-Length: " + std::to_string(response.body.size()) + "\r\nConnection: close\r\n\r\n";
    return head ? out : out + response.body;
}

std::atomic<bool> serving{false};

void serve(AsyncWebServer *server, int listener) {
    std::vector<Connection> connections;
    while (true) {
        std::vector<pollfd> entries{{listener, POLLIN, 0}};
        for (const auto &connection : connections)
            entries.push_back({connection.fd, POLLIN, 0});
        if (::poll(entries.data(), entries.size(), 1000) < 0 && errno != EINTR)
            continue;
        if (entries[0].revents & POLLIN) {
            int fd = ::accept4(listener, nullptr, nullptr, SOCK_NONBLOCK | SOCK_CLOEXEC);
            if (fd >= 0)
                connections.push_back({fd, "", std::chrono::steady_clock::now()});
        }
        auto now = std::chrono::steady_clock::now();
        for (size_t i = connections.size(); i-- > 0;) {
            Connection &connection = connections[i];
            bool readable =
                i + 1 < entries.size() && (entries[i + 1].revents & (POLLIN | POLLHUP | POLLERR));
            bool close = false;
            if (readable) {
                char buffer[8192];
                ssize_t count = ::recv(connection.fd, buffer, sizeof(buffer), 0);
                if (count > 0) {
                    connection.data.append(buffer, size_t(count));
                    connection.last = now;
                } else if (count == 0 || (errno != EAGAIN && errno != EINTR)) {
                    close = true;
                }
            }
            if (!close) {
                Parsed request = parse(connection.data);
                if (request.complete || request.too_large) {
                    AsyncWebServerRequest::Response response;
                    if (request.too_large || request.invalid || request.unsupported ||
                        !request.method) {
                        response.code = request.too_large                   ? 413
                                        : request.unsupported               ? 501
                                        : request.method || request.invalid ? 400
                                                                            : 501;
                    } else {
                        AsyncWebServerRequest call(request.method, request.url,
                                                   request.content_type, request.body);
                        server->dispatch(call);
                        response = call.result();
                    }
                    send_all(connection.fd, serialize(response, request.method == HTTP_HEAD));
                    close = true;
                } else if (now - connection.last > kIdle) {
                    close = true;
                }
            }
            if (close) {
                ::close(connection.fd);
                connections.erase(connections.begin() + long(i));
            }
        }
    }
}

} // namespace

void AsyncWebServer::begin() {
    if (started)
        return;
    started = true;
    int listener = flapsim::host::config().http_fd;
    /* Only the device's port 80 is forwarded, and one server task serves it per boot. */
    if (port != 80 || listener < 0 || serving.exchange(true))
        return;
    std::thread(serve, this, listener).detach();
}

void AsyncWebServer::end() { started = false; }
