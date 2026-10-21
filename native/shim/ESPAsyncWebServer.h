/* SPDX-License-Identifier: GPL-2.0-only */
/* Handler-API adapter for mathieucarbou/ESPAsyncWebServer 3.x. Like the device, every handler
 * runs on one server task, concurrently with loop(). */
#pragma once
#include "Arduino.h"
#include "FS.h"

#include <functional>
#include <map>
#include <memory>
#include <vector>

typedef enum {
    HTTP_GET = 0b00000001,
    HTTP_POST = 0b00000010,
    HTTP_DELETE = 0b00000100,
    HTTP_PUT = 0b00001000,
    HTTP_PATCH = 0b00010000,
    HTTP_HEAD = 0b00100000,
    HTTP_OPTIONS = 0b01000000,
    HTTP_ANY = 0b01111111,
} WebRequestMethod;
typedef uint8_t WebRequestMethodComposite;

class AsyncWebServerRequest;
typedef std::function<void(AsyncWebServerRequest *request)> ArRequestHandlerFunction;

class AsyncWebServerRequest {
  public:
    AsyncWebServerRequest(WebRequestMethodComposite method, String url, String content_type,
                          String body);
    WebRequestMethodComposite method() const { return request_method; }
    const char *methodToString() const;
    const String &url() const { return request_url; }
    const String &contentType() const { return request_content_type; }
    size_t contentLength() const { return request_body.length(); }
    const String &body() const { return request_body; }
    bool isHTTP() const { return true; }

    void send(int code, const char *content_type = "", const String &content = String());
    void send(int code, const String &content_type, const String &content = String()) {
        send(code, content_type.c_str(), content);
    }
    void redirect(const char *url, int code = 302);
    void sendFile(int code, const String &content_type, const std::string &bytes,
                  std::vector<std::pair<String, String>> headers);

    /* Filled by handlers, read by the native server. */
    struct Response {
        int code = 0;
        String content_type;
        std::string body;
        std::vector<std::pair<String, String>> headers;
    };
    bool responded() const { return response.code != 0; }
    const Response &result() const { return response; }

  private:
    WebRequestMethodComposite request_method;
    String request_url, request_content_type, request_body;
    Response response;
};

class AsyncWebHandler {
  public:
    virtual ~AsyncWebHandler() = default;
    virtual bool canHandle(AsyncWebServerRequest *request) const = 0;
    virtual void handleRequest(AsyncWebServerRequest *request) = 0;
};

class AsyncCallbackWebHandler : public AsyncWebHandler {
  public:
    AsyncCallbackWebHandler(String uri, WebRequestMethodComposite method,
                            ArRequestHandlerFunction handler)
        : uri(std::move(uri)), methods(method), handler(std::move(handler)) {}
    bool canHandle(AsyncWebServerRequest *request) const override;
    void handleRequest(AsyncWebServerRequest *request) override;

  private:
    String uri;
    WebRequestMethodComposite methods;
    ArRequestHandlerFunction handler;
};

class AsyncStaticWebHandler : public AsyncWebHandler {
  public:
    AsyncStaticWebHandler(String uri, fs::FS &filesystem, String path, String cache_control)
        : uri(std::move(uri)), filesystem(filesystem), path(std::move(path)),
          cache(std::move(cache_control)) {}
    AsyncStaticWebHandler &setCacheControl(const char *value) {
        cache = value;
        return *this;
    }
    AsyncStaticWebHandler &setDefaultFile(const char *value) {
        default_file = value;
        return *this;
    }
    bool canHandle(AsyncWebServerRequest *request) const override;
    void handleRequest(AsyncWebServerRequest *request) override;

  private:
    String uri;
    fs::FS &filesystem;
    String path, cache, default_file = "index.htm";
};

class AsyncWebServer {
  public:
    explicit AsyncWebServer(uint16_t port) : port(port) {}
    ~AsyncWebServer();
    AsyncCallbackWebHandler &on(const char *uri, WebRequestMethodComposite method,
                                ArRequestHandlerFunction handler);
    AsyncCallbackWebHandler &on(const char *uri, ArRequestHandlerFunction handler) {
        return on(uri, HTTP_ANY, std::move(handler));
    }
    AsyncStaticWebHandler &serveStatic(const char *uri, fs::FS &filesystem, const char *path,
                                       const char *cache_control = nullptr);
    AsyncWebHandler &addHandler(AsyncWebHandler *handler);
    void onNotFound(ArRequestHandlerFunction handler) { not_found = std::move(handler); }
    void begin();
    void end();
    /* Runs one request through the handler chain on the calling (server) thread. */
    void dispatch(AsyncWebServerRequest &request);
    uint16_t devicePort() const { return port; }

  private:
    uint16_t port;
    std::vector<std::unique_ptr<AsyncWebHandler>> handlers;
    ArRequestHandlerFunction not_found;
    bool started = false;
};
