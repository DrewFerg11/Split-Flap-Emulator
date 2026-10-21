/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include "ESPAsyncWebServer.h"

#include <ArduinoJson.h>

typedef std::function<void(AsyncWebServerRequest *request, JsonVariant &json)>
    ArJsonRequestHandlerFunction;

/* ESPAsyncWebServer 3.6 AsyncCallbackJsonWebHandler: GET/POST/PUT/PATCH by default; non-GET
 * requests need an application/json body of at most 16 KiB, else 413 (too large) or 400. */
class AsyncCallbackJsonWebHandler : public AsyncWebHandler {
  public:
    AsyncCallbackJsonWebHandler(const String &uri, ArJsonRequestHandlerFunction handler)
        : uri(uri), handler(std::move(handler)) {}
    void setMethod(WebRequestMethodComposite value) { methods = value; }
    void setMaxContentLength(size_t value) { max_length = value; }
    void onRequest(ArJsonRequestHandlerFunction value) { handler = std::move(value); }
    bool canHandle(AsyncWebServerRequest *request) const override;
    void handleRequest(AsyncWebServerRequest *request) override;

  private:
    String uri;
    ArJsonRequestHandlerFunction handler;
    WebRequestMethodComposite methods = HTTP_GET | HTTP_POST | HTTP_PUT | HTTP_PATCH;
    size_t max_length = 16384;
};
