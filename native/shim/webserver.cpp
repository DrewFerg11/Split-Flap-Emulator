/* SPDX-License-Identifier: GPL-2.0-only */
/* Request routing with ESPAsyncWebServer 3.6 semantics: handlers are tried in registration order
 * and the first whose canHandle() accepts the request handles it. */
#include "AsyncJson.h"
#include "ESPAsyncWebServer.h"

#include <cstring>

namespace {

const char *content_type_for(const String &path) {
    static const std::pair<const char *, const char *> types[] = {
        {".html", "text/html"},
        {".htm", "text/html"},
        {".css", "text/css"},
        {".json", "application/json"},
        {".js", "application/javascript"},
        {".png", "image/png"},
        {".gif", "image/gif"},
        {".jpg", "image/jpeg"},
        {".ico", "image/x-icon"},
        {".svg", "image/svg+xml"},
        {".eot", "font/eot"},
        {".woff", "font/woff"},
        {".woff2", "font/woff2"},
        {".ttf", "font/ttf"},
        {".xml", "text/xml"},
        {".pdf", "application/pdf"},
        {".zip", "application/zip"},
        {".gz", "application/x-gzip"},
    };
    for (const auto &[suffix, type] : types) {
        if (path.endsWith(suffix))
            return type;
    }
    return "text/plain";
}

} // namespace

AsyncWebServerRequest::AsyncWebServerRequest(WebRequestMethodComposite method, String url,
                                             String content_type, String body)
    : request_method(method), request_url(std::move(url)),
      request_content_type(std::move(content_type)), request_body(std::move(body)) {}

const char *AsyncWebServerRequest::methodToString() const {
    switch (request_method) {
    case HTTP_GET:
        return "GET";
    case HTTP_POST:
        return "POST";
    case HTTP_DELETE:
        return "DELETE";
    case HTTP_PUT:
        return "PUT";
    case HTTP_PATCH:
        return "PATCH";
    case HTTP_HEAD:
        return "HEAD";
    case HTTP_OPTIONS:
        return "OPTIONS";
    default:
        return "UNKNOWN";
    }
}

void AsyncWebServerRequest::send(int code, const char *content_type, const String &content) {
    if (responded())
        return;
    response.code = code;
    response.content_type = content_type ? content_type : "";
    response.body.assign(content.c_str(), content.length());
}

void AsyncWebServerRequest::redirect(const char *url, int code) {
    if (responded())
        return;
    response.code = code;
    response.headers.emplace_back("Location", url ? url : "/");
}

void AsyncWebServerRequest::sendFile(int code, const String &content_type, const std::string &bytes,
                                     std::vector<std::pair<String, String>> headers) {
    if (responded())
        return;
    response.code = code;
    response.content_type = content_type;
    response.body = bytes;
    response.headers = std::move(headers);
}

bool AsyncCallbackWebHandler::canHandle(AsyncWebServerRequest *request) const {
    if (!handler || !(methods & request->method()))
        return false;
    const String &url = request->url();
    if (uri.startsWith("/*."))
        return url.endsWith(uri.substring(uri.lastIndexOf('.')));
    if (uri.endsWith("*"))
        return url.startsWith(uri.substring(0, uri.length() - 1));
    return uri.isEmpty() || uri == url || url.startsWith(uri + "/");
}

void AsyncCallbackWebHandler::handleRequest(AsyncWebServerRequest *request) {
    if (handler)
        handler(request);
    else
        request->send(500);
}

/* A file wins over its .gz twin; a directory request falls back to the default file. */
bool AsyncStaticWebHandler::canHandle(AsyncWebServerRequest *request) const {
    if (request->method() != HTTP_GET || !request->url().startsWith(uri))
        return false;
    String rest = request->url().substring(uri.length());
    String candidate = path + rest;
    auto real = [this](const String &name) {
        if (!filesystem.exists(name))
            return false;
        File file = filesystem.open(name, "r");
        return bool(file) && !file.isDirectory();
    };
    bool directory_hint = rest.length() && rest[rest.length() - 1] == '/';
    if (!directory_hint && (real(candidate) || real(candidate + ".gz")))
        return true;
    if (default_file.isEmpty())
        return false;
    if (!candidate.endsWith("/"))
        candidate += "/";
    candidate += default_file;
    return real(candidate) || real(candidate + ".gz");
}

void AsyncStaticWebHandler::handleRequest(AsyncWebServerRequest *request) {
    String rest = request->url().substring(uri.length());
    String candidates[] = {path + rest, path + rest + "/" + default_file};
    for (const String &name : candidates) {
        for (bool gzip : {false, true}) {
            String stored = gzip ? name + ".gz" : name;
            if (!filesystem.exists(stored))
                continue;
            File file = filesystem.open(stored, "r");
            if (!file || file.isDirectory())
                continue;
            std::string bytes(file.size(), '\0');
            bytes.resize(file.read(reinterpret_cast<uint8_t *>(bytes.data()), bytes.size()));
            std::vector<std::pair<String, String>> headers{
                {"ETag", String((unsigned long)bytes.size())}};
            if (gzip)
                headers.emplace_back("Content-Encoding", "gzip");
            if (!cache.isEmpty())
                headers.emplace_back("Cache-Control", cache);
            request->sendFile(200, content_type_for(name), bytes, headers);
            return;
        }
    }
    request->send(404);
}

bool AsyncCallbackJsonWebHandler::canHandle(AsyncWebServerRequest *request) const {
    if (!handler || !(methods & request->method()))
        return false;
    if (!uri.isEmpty() && uri != request->url() && !request->url().startsWith(uri + "/"))
        return false;
    return request->method() == HTTP_GET ||
           request->contentType().equalsIgnoreCase("application/json");
}

void AsyncCallbackJsonWebHandler::handleRequest(AsyncWebServerRequest *request) {
    if (!handler) {
        request->send(500);
        return;
    }
    if (request->method() == HTTP_GET) {
        JsonVariant json;
        handler(request, json);
        return;
    }
    /* The library buffers a body only when it is nonempty and below the maximum length. */
    size_t length = request->contentLength();
    if (length > 0 && length < max_length) {
        JsonDocument document;
        if (!deserializeJson(document, request->body().c_str(), length)) {
            JsonVariant json = document.as<JsonVariant>();
            handler(request, json);
            return;
        }
    }
    request->send(length > max_length ? 413 : 400);
}

AsyncWebServer::~AsyncWebServer() { end(); }

AsyncCallbackWebHandler &AsyncWebServer::on(const char *uri, WebRequestMethodComposite method,
                                            ArRequestHandlerFunction handler) {
    auto *added = new AsyncCallbackWebHandler(uri, method, std::move(handler));
    handlers.emplace_back(added);
    return *added;
}

AsyncStaticWebHandler &AsyncWebServer::serveStatic(const char *uri, fs::FS &filesystem,
                                                   const char *path, const char *cache_control) {
    String route = uri && uri[0] == '/' ? String(uri) : String("/") + uri;
    String file = path && path[0] == '/' ? String(path) : String("/") + path;
    if (route.endsWith("/"))
        route = route.substring(0, route.length() - 1);
    if (file.endsWith("/"))
        file = file.substring(0, file.length() - 1);
    auto *added =
        new AsyncStaticWebHandler(route, filesystem, file, cache_control ? cache_control : "");
    handlers.emplace_back(added);
    return *added;
}

AsyncWebHandler &AsyncWebServer::addHandler(AsyncWebHandler *handler) {
    handlers.emplace_back(handler);
    return *handler;
}

void AsyncWebServer::dispatch(AsyncWebServerRequest &request) {
    for (auto &handler : handlers) {
        if (handler->canHandle(&request)) {
            handler->handleRequest(&request);
            if (!request.responded())
                request.send(500);
            return;
        }
    }
    if (not_found)
        not_found(&request);
    if (!request.responded())
        request.send(404);
}

/* This phase has no host listener yet; the network transport arrives with --mode native. */
void AsyncWebServer::begin() { started = true; }

void AsyncWebServer::end() { started = false; }
