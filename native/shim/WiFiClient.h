/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include "Client.h"

#include <memory>

/* TCP client on a host socket. Copies share one connection, as arduino-esp32's do. Connect and
 * write use the client's timeout in virtual milliseconds; reads never block. */
class WiFiClient : public Client {
  public:
    WiFiClient();
    ~WiFiClient() override;
    WiFiClient(const WiFiClient &) = default;
    WiFiClient &operator=(const WiFiClient &) = default;

    int connect(IPAddress ip, uint16_t port) override;
    int connect(IPAddress ip, uint16_t port, int32_t timeout_ms);
    int connect(const char *host, uint16_t port) override;
    int connect(const char *host, uint16_t port, int32_t timeout_ms);
    using Print::write;
    size_t write(uint8_t value) override { return write(&value, 1); }
    size_t write(const uint8_t *buffer, size_t size) override;
    int available() override;
    int read() override;
    int read(uint8_t *buffer, size_t size) override;
    int peek() override;
    void flush() override {}
    void stop() override;
    uint8_t connected() override;
    operator bool() override { return connected(); }
    void setNoDelay(bool) {}
    int fd() const;

  private:
    struct Socket;
    std::shared_ptr<Socket> socket;
    int connect_timeout_ms = 3000;
};
