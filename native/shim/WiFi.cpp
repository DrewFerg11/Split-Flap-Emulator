/* SPDX-License-Identifier: GPL-2.0-only */
#include "WiFi.h"

#include "host.h"
#include "vclock.h"

WiFiClass WiFi;

namespace {
using Guard = std::lock_guard<std::recursive_mutex>;
}

void WiFiClass::update(int64_t now) {
    const auto &settings = flapsim::host::config().wifi;
    if (joining && settings.station_connects &&
        now - join_started_ns >= settings.connect_delay_ms * 1000000) {
        joining = false;
        associated = true;
    }
}

bool WiFiClass::mode(wifi_mode_t next) {
    Guard guard(lock);
    current_mode = next;
    if (!(next & WIFI_STA))
        joining = associated = false;
    if (!(next & WIFI_AP))
        access_point = false;
    return true;
}

wifi_mode_t WiFiClass::getMode() {
    Guard guard(lock);
    return current_mode;
}

wl_status_t WiFiClass::begin(const char *network, const char *key, int32_t, const uint8_t *,
                             bool connect) {
    Guard guard(lock);
    if (!network || !*network)
        return WL_CONNECT_FAILED;
    current_mode = wifi_mode_t(current_mode | WIFI_STA);
    ssid = network;
    password = key ? key : "";
    associated = false;
    joining = connect;
    join_started_ns = flapsim::vclock::now_ns();
    return status();
}

wl_status_t WiFiClass::status() {
    Guard guard(lock);
    if (!(current_mode & WIFI_STA))
        return WL_NO_SHIELD;
    update(flapsim::vclock::now_ns());
    return associated ? WL_CONNECTED : WL_DISCONNECTED;
}

bool WiFiClass::disconnect(bool wifi_off, bool erase_ap) {
    Guard guard(lock);
    joining = associated = false;
    if (erase_ap)
        ssid = password = "";
    if (wifi_off)
        current_mode = wifi_mode_t(current_mode & ~WIFI_STA);
    return true;
}

bool WiFiClass::reconnect() {
    Guard guard(lock);
    if (!(current_mode & WIFI_STA) || ssid.isEmpty())
        return false;
    associated = false;
    joining = true;
    join_started_ns = flapsim::vclock::now_ns();
    return true;
}

bool WiFiClass::setAutoReconnect(bool enabled) {
    Guard guard(lock);
    auto_reconnect = enabled;
    return true;
}

bool WiFiClass::getAutoReconnect() {
    Guard guard(lock);
    return auto_reconnect;
}

bool WiFiClass::setHostname(const char *name) {
    Guard guard(lock);
    hostname = name ? name : "";
    return true;
}

const char *WiFiClass::getHostname() {
    Guard guard(lock);
    return hostname.c_str();
}

IPAddress WiFiClass::localIP() {
    IPAddress address;
    if (status() == WL_CONNECTED)
        address.fromString(flapsim::host::config().wifi.ip.c_str());
    return address;
}

String WiFiClass::SSID() {
    Guard guard(lock);
    return associated ? ssid : String();
}

bool WiFiClass::softAP(const char *network, const char *, int, int, int, bool) {
    Guard guard(lock);
    if (!network || !*network)
        return false;
    current_mode = wifi_mode_t(current_mode | WIFI_AP);
    ap_ssid = network;
    access_point = true;
    return true;
}

bool WiFiClass::softAPdisconnect(bool wifi_off) {
    Guard guard(lock);
    access_point = false;
    if (wifi_off)
        current_mode = wifi_mode_t(current_mode & ~WIFI_AP);
    return true;
}

IPAddress WiFiClass::softAPIP() {
    Guard guard(lock);
    return access_point ? IPAddress(192, 168, 4, 1) : IPAddress();
}

/* One visible network, so Improv's scan has a deterministic answer. */
int16_t WiFiClass::scanNetworks(bool, bool) { return 1; }
String WiFiClass::SSID(uint8_t index) { return index == 0 ? String("flapsim") : String(); }
int32_t WiFiClass::RSSI(uint8_t index) { return index == 0 ? -50 : 0; }
wifi_auth_mode_t WiFiClass::encryptionType(uint8_t) { return WIFI_AUTH_WPA2_PSK; }
