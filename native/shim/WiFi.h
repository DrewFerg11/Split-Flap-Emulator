/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include "IPAddress.h"
#include "WString.h"
#include "WiFiClient.h"

#include <cstdint>
#include <mutex>

typedef enum {
    WL_NO_SHIELD = 255,
    WL_IDLE_STATUS = 0,
    WL_NO_SSID_AVAIL = 1,
    WL_SCAN_COMPLETED = 2,
    WL_CONNECTED = 3,
    WL_CONNECT_FAILED = 4,
    WL_CONNECTION_LOST = 5,
    WL_DISCONNECTED = 6
} wl_status_t;

typedef enum { WIFI_OFF = 0, WIFI_STA = 1, WIFI_AP = 2, WIFI_AP_STA = 3 } wifi_mode_t;
#define WIFI_MODE_NULL WIFI_OFF
#define WIFI_MODE_STA WIFI_STA
#define WIFI_MODE_AP WIFI_AP
#define WIFI_MODE_APSTA WIFI_AP_STA

typedef enum {
    WIFI_AUTH_OPEN = 0,
    WIFI_AUTH_WEP,
    WIFI_AUTH_WPA_PSK,
    WIFI_AUTH_WPA2_PSK
} wifi_auth_mode_t;

typedef enum {
    WIFI_POWER_19_5dBm = 78,
    WIFI_POWER_11dBm = 44,
    WIFI_POWER_8_5dBm = 34,
    WIFI_POWER_2dBm = 8,
} wifi_power_t;

/* Wi-Fi without a radio. The access point always starts at 192.168.4.1 (the runner forwards
 * the device's port 80, as QEMU's hostfwd does). Station mode "associates" with any network
 * after the configured virtual delay and reports the configured host address, or never
 * associates in the configured failure mode. */
class WiFiClass {
  public:
    bool mode(wifi_mode_t mode);
    wifi_mode_t getMode();
    wl_status_t begin(const char *ssid, const char *password = nullptr, int32_t channel = 0,
                      const uint8_t *bssid = nullptr, bool connect = true);
    wl_status_t begin() { return begin(ssid.c_str(), password.c_str()); }
    wl_status_t status();
    bool disconnect(bool wifi_off = false, bool erase_ap = false);
    bool reconnect();
    bool isConnected() { return status() == WL_CONNECTED; }
    bool setAutoReconnect(bool enabled);
    bool getAutoReconnect();
    void persistent(bool) {}
    bool setSleep(bool) { return true; }
    bool setTxPower(wifi_power_t) { return true; }
    bool setHostname(const char *name);
    const char *getHostname();
    IPAddress localIP();
    String SSID();
    String macAddress() { return "24:0A:C4:00:01:10"; }

    bool softAP(const char *ssid, const char *password = nullptr, int channel = 1, int hidden = 0,
                int max_connections = 4, bool ftm = false);
    bool softAPdisconnect(bool wifi_off = false);
    IPAddress softAPIP();
    String softAPmacAddress() { return "24:0A:C4:00:01:11"; }

    int16_t scanNetworks(bool async = false, bool show_hidden = false);
    String SSID(uint8_t index);
    int32_t RSSI(uint8_t index);
    wifi_auth_mode_t encryptionType(uint8_t index);

  private:
    void update(int64_t now);
    std::recursive_mutex lock;
    wifi_mode_t current_mode = WIFI_OFF;
    bool joining = false, associated = false, access_point = false, auto_reconnect = true;
    int64_t join_started_ns = 0;
    String ssid, password, ap_ssid, hostname = "esp32-arduino";
};

extern WiFiClass WiFi;
