/* SPDX-License-Identifier: GPL-2.0-only */
#include "WiFiClient.h"

#include "WiFi.h"
#include "esp32-hal-log.h"
#include "vclock.h"

#include <algorithm>
#include <arpa/inet.h>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <fcntl.h>
#include <future>
#include <mutex>
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

/* Socket waits keep one guest deadline and re-evaluate it on bounded host slices. */
int socket_event(int fd, short events, int timeout_ms) {
    pollfd entry{fd, events, 0};
    int64_t target = flapsim::vclock::now_ns() + std::max<int64_t>(0, timeout_ms) * 1000000;
    do {
        auto duration = flapsim::vclock::wait_slice(target - flapsim::vclock::now_ns());
        int milliseconds = int(std::chrono::ceil<std::chrono::milliseconds>(duration).count());
        int result = ::poll(&entry, 1, milliseconds);
        if (result > 0)
            return entry.revents;
        if (result < 0 && errno != EINTR)
            return 0;
    } while (flapsim::vclock::now_ns() < target);
    return 0;
}

bool wait_for(int fd, short events, int timeout_ms) {
    int event = socket_event(fd, events, timeout_ms);
    return !(event & (POLLERR | POLLNVAL)) && (event & events);
}

bool ready(int fd, short events, int timeout_ms) { return socket_event(fd, events, timeout_ms); }

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

/* As arduino-esp32 2.0.17's hostByName(): one lookup at a time, waiting up to 16 s for an
 * earlier one to finish and then up to 15 s for its own answer, independently of the connect
 * timeout. A lookup still running at its deadline keeps its thread until it returns; where the
 * device would then start another lookup anyway, the shim fails it, so a hung resolver holds at
 * most one thread. */
constexpr int64_t kDnsIdleTimeoutNs = 16'000'000'000, kDnsTimeoutNs = 15'000'000'000;

struct Lookups {
    std::mutex mutex;
    std::condition_variable idle;
    bool busy = false;
};

/* Never destroyed: an abandoned lookup thread may still finish during process exit. */
Lookups &lookups() {
    static Lookups *state = new Lookups;
    return *state;
}

std::optional<uint32_t> lookup(const char *host) {
    Lookups &state = lookups();
    {
        std::unique_lock lock(state.mutex);
        if (!flapsim::vclock::wait_for(state.idle, lock, kDnsIdleTimeoutNs,
                                       [&] { return !state.busy; }))
            return std::nullopt;
        state.busy = true;
    }
    auto answer = std::make_shared<std::promise<std::optional<uint32_t>>>();
    auto result = answer->get_future();
    std::thread([answer, &state, name = std::string(host), find = resolver.load()] {
        uint32_t address = 0;
        bool found = find(name.c_str(), &address);
        {
            std::lock_guard lock(state.mutex);
            state.busy = false;
        }
        state.idle.notify_all();
        answer->set_value(found ? std::optional(address) : std::nullopt);
    }).detach();
    int64_t target = flapsim::vclock::now_ns() + kDnsTimeoutNs;
    while (result.wait_for(flapsim::vclock::wait_slice(target - flapsim::vclock::now_ns())) !=
           std::future_status::ready) {
        if (flapsim::vclock::now_ns() >= target)
            return std::nullopt;
    }
    return result.get();
}

std::optional<uint32_t> resolve(const char *host) {
    in_addr numeric{};
    if (::inet_pton(AF_INET, host, &numeric) == 1)
        return numeric.s_addr;
    std::optional<uint32_t> address = lookup(host);
    if (!address)
        flapsim_log('E', "WiFiGeneric.cpp", 1583, "hostByName", "DNS Failed for %s", host);
    return address;
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

/* As arduino-esp32 2.0.17: each wait for room lasts 1 s, ten waits in a row without progress give
 * up, and any progress resets the count, so a slowly draining peer is followed for as long as data
 * keeps moving. A socket error ends the connection. */
size_t WiFiClient::write(const uint8_t *buffer, size_t size) {
    constexpr int kWriteRetries = 10, kWriteWaitMs = 1000;
    size_t sent = 0;
    for (int retries = kWriteRetries; socket && sent < size && retries > 0; retries--) {
        if (!ready(socket->fd, POLLOUT, kWriteWaitMs))
            continue;
        ssize_t count = ::send(socket->fd, buffer + sent, size - sent, MSG_NOSIGNAL);
        if (count > 0) {
            sent += size_t(count);
            retries = kWriteRetries + 1;
        } else if (count < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
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
