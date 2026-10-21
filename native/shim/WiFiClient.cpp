/* SPDX-License-Identifier: GPL-2.0-only */
#include "WiFiClient.h"

#include "WiFi.h"
#include "vclock.h"

#include <arpa/inet.h>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <fcntl.h>
#include <future>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <optional>
#include <poll.h>
#include <string>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>

struct WiFiClient::Socket {
    int fd;
    explicit Socket(int fd) : fd(fd) {}
    ~Socket() { ::close(fd); }
};

namespace {

/* Wait for a socket event for at most `timeout_ms` of virtual time. */
bool wait_for(int fd, short events, int timeout_ms) {
    pollfd entry{fd, events, 0};
    auto host = flapsim::vclock::host_duration(int64_t(timeout_ms) * 1000000);
    int milliseconds = int(std::chrono::ceil<std::chrono::milliseconds>(host).count());
    while (true) {
        int ready = ::poll(&entry, 1, milliseconds);
        if (ready < 0 && errno == EINTR)
            continue;
        return ready > 0 && !(entry.revents & (POLLERR | POLLNVAL)) && (entry.revents & events);
    }
}

/* The device only reaches other hosts once it has joined a network as a station. */
bool station_online() { return WiFi.status() == WL_CONNECTED; }

bool host_resolver(const char *host, uint32_t *address) {
    addrinfo hints{}, *result = nullptr;
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    bool found = ::getaddrinfo(host, nullptr, &hints, &result) == 0 && result;
    if (found)
        *address = reinterpret_cast<sockaddr_in *>(result->ai_addr)->sin_addr.s_addr;
    if (result)
        ::freeaddrinfo(result);
    return found;
}

std::atomic<flapsim::net::Resolver> resolver{host_resolver};

/* As arduino-esp32 2.0.17's hostByName(): a lookup waits at most 15 s, independently of the
 * connect timeout. A lookup still running then is abandoned on its own thread. */
constexpr int64_t kDnsTimeoutNs = 15'000'000'000;

std::optional<uint32_t> resolve(const char *host) {
    in_addr numeric{};
    if (::inet_pton(AF_INET, host, &numeric) == 1)
        return numeric.s_addr;
    auto answer = std::make_shared<std::promise<std::optional<uint32_t>>>();
    auto result = answer->get_future();
    std::thread([answer, name = std::string(host), lookup = resolver.load()] {
        uint32_t address = 0;
        answer->set_value(lookup(name.c_str(), &address) ? std::optional(address) : std::nullopt);
    }).detach();
    if (result.wait_for(flapsim::vclock::host_duration(kDnsTimeoutNs)) != std::future_status::ready)
        return std::nullopt;
    return result.get();
}

} // namespace

void flapsim::net::set_resolver(Resolver lookup) { resolver = lookup ? lookup : host_resolver; }

WiFiClient::WiFiClient() = default;
WiFiClient::~WiFiClient() = default;

int WiFiClient::fd() const { return socket ? socket->fd : -1; }

int WiFiClient::connect(IPAddress ip, uint16_t port) {
    return connect(ip, port, connect_timeout_ms);
}

int WiFiClient::connect(IPAddress ip, uint16_t port, int32_t timeout_ms) {
    stop();
    if (!station_online())
        return 0;
    int fd = ::socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd < 0)
        return 0;
    auto owned = std::make_shared<Socket>(fd);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(port);
    address.sin_addr.s_addr = uint32_t(ip);
    if (::connect(fd, reinterpret_cast<sockaddr *>(&address), sizeof(address)) != 0) {
        if (errno != EINPROGRESS || !wait_for(fd, POLLOUT, timeout_ms))
            return 0;
        int error = 0;
        socklen_t length = sizeof(error);
        if (::getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &length) != 0 || error != 0)
            return 0;
    }
    int one = 1;
    ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    socket = owned;
    return 1;
}

int WiFiClient::connect(const char *host, uint16_t port) {
    return connect(host, port, connect_timeout_ms);
}

int WiFiClient::connect(const char *host, uint16_t port, int32_t timeout_ms) {
    if (!host || !station_online())
        return 0;
    std::optional<uint32_t> address = resolve(host);
    return address ? connect(IPAddress(*address), port, timeout_ms) : 0;
}

size_t WiFiClient::write(const uint8_t *buffer, size_t size) {
    size_t sent = 0;
    while (socket && sent < size) {
        ssize_t count = ::send(socket->fd, buffer + sent, size - sent, MSG_NOSIGNAL);
        if (count > 0) {
            sent += size_t(count);
        } else if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) {
            if (!wait_for(socket->fd, POLLOUT, int(timeout)))
                break;
        } else {
            stop();
        }
    }
    return sent;
}

int WiFiClient::available() {
    if (!socket)
        return 0;
    int count = 0;
    if (::ioctl(socket->fd, FIONREAD, &count) != 0)
        return 0;
    return count;
}

int WiFiClient::read() {
    uint8_t value;
    return read(&value, 1) == 1 ? value : -1;
}

int WiFiClient::read(uint8_t *buffer, size_t size) {
    if (!socket)
        return -1;
    ssize_t count = ::recv(socket->fd, buffer, size, MSG_DONTWAIT);
    if (count > 0)
        return int(count);
    if (count == 0 || (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR))
        stop();
    return -1;
}

int WiFiClient::peek() {
    uint8_t value;
    if (!socket || ::recv(socket->fd, &value, 1, MSG_PEEK | MSG_DONTWAIT) != 1)
        return -1;
    return value;
}

void WiFiClient::stop() { socket.reset(); }

/* Like arduino-esp32: still "connected" while unread data remains after the peer closed. */
uint8_t WiFiClient::connected() {
    if (!socket)
        return 0;
    uint8_t probe;
    ssize_t count = ::recv(socket->fd, &probe, 1, MSG_PEEK | MSG_DONTWAIT);
    if (count > 0)
        return 1;
    if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
        return 1;
    stop();
    return 0;
}
