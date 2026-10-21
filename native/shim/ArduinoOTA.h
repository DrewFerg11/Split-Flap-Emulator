/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include "Update.h"
#include "WString.h"

#include <functional>

typedef enum {
    OTA_AUTH_ERROR,
    OTA_BEGIN_ERROR,
    OTA_CONNECT_ERROR,
    OTA_RECEIVE_ERROR,
    OTA_END_ERROR
} ota_error_t;

/* espota uploads are not emulated yet: OTA is configured and polled, but never receives. */
class ArduinoOTAClass {
  public:
    typedef std::function<void(void)> THandlerFunction;
    typedef std::function<void(ota_error_t)> THandlerFunction_Error;
    typedef std::function<void(unsigned int, unsigned int)> THandlerFunction_Progress;

    ArduinoOTAClass &setPort(uint16_t value) {
        port = value;
        return *this;
    }
    ArduinoOTAClass &setHostname(const char *value) {
        hostname = value ? value : "";
        return *this;
    }
    String getHostname() { return hostname; }
    ArduinoOTAClass &setPassword(const char *value) {
        password = value ? value : "";
        return *this;
    }
    ArduinoOTAClass &setPasswordHash(const char *value) { return setPassword(value); }
    ArduinoOTAClass &setRebootOnSuccess(bool value) {
        reboot = value;
        return *this;
    }
    ArduinoOTAClass &setMdnsEnabled(bool) { return *this; }
    ArduinoOTAClass &onStart(THandlerFunction handler) {
        start = std::move(handler);
        return *this;
    }
    ArduinoOTAClass &onEnd(THandlerFunction handler) {
        finish = std::move(handler);
        return *this;
    }
    ArduinoOTAClass &onError(THandlerFunction_Error handler) {
        error = std::move(handler);
        return *this;
    }
    ArduinoOTAClass &onProgress(THandlerFunction_Progress handler) {
        progress = std::move(handler);
        return *this;
    }
    void begin() { started = true; }
    void end() { started = false; }
    void handle() {}
    int getCommand() { return U_FLASH; }

  private:
    String hostname, password;
    uint16_t port = 3232;
    bool reboot = true, started = false;
    THandlerFunction start, finish;
    THandlerFunction_Error error;
    THandlerFunction_Progress progress;
};

extern ArduinoOTAClass ArduinoOTA;
