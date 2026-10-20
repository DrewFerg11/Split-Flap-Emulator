"""End-to-end POC scenario: provision Wi-Fi through the firmware's own AP-mode web UI,
switch to single-bus mode, then drive the display over HTTP and MQTT and check the drums."""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

P = os.path.dirname(os.path.abspath(__file__))
RUN = os.path.join(P, "runs", sys.argv[1] if len(sys.argv) > 1 else "net2")
EXTRA = sys.argv[2:]  # extra qemu args, e.g. -icount shift=3,sleep=off
os.makedirs(RUN, exist_ok=True)
shutil.copy(f"{P}/fw/esp32_n4-v1.4.0-flash4M.bin", f"{RUN}/flash.bin")
SERIAL, STATE = f"{RUN}/serial.log", f"{RUN}/state.json"
for f in (SERIAL, STATE):
    if os.path.exists(f):
        os.remove(f)
T0 = time.time()


def log(msg):
    print(f"[{time.time() - T0:6.1f}s wall] {msg}", flush=True)


def serial():
    try:
        return open(SERIAL, errors="replace").read()
    except FileNotFoundError:
        return ""


def wait_for(pred, what, timeout):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(1)
    raise SystemExit(f"FAIL: timed out waiting for {what}")


def http(port, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.status, r.read().decode()


def display():
    try:
        st = json.load(open(STATE))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return "".join(m["char"] for m in st["modules"]), st["t"]


# MQTT broker on the host. The guest reaches it as 192.168.4.2 (the slirp gateway), which slirp
# forwards to the host's loopback, so the broker only needs to listen on 127.0.0.1.
open(f"{RUN}/mosquitto.conf", "w").write("listener 1883 127.0.0.1\nallow_anonymous true\n")
broker = subprocess.Popen(["mosquitto", "-c", f"{RUN}/mosquitto.conf"], stderr=subprocess.DEVNULL)
time.sleep(1)
sub = subprocess.Popen(["mosquitto_sub", "-h", "127.0.0.1", "-v", "-t", "splitflap/#"], stdout=open(f"{RUN}/mqtt.log", "w"))

env = dict(os.environ, FLAPSIM_BUS1="", FLAPSIM_SEED="7", FLAPSIM_STATE=STATE, FLAPSIM_TIMELINE=f"{RUN}/timeline.jsonl")
qemu = subprocess.Popen(
    [f"{P}/qemu-lcgamboa/build/qemu-system-xtensa", "-M", "esp32-picsimlab", *EXTRA, "-L", f"{P}/qemu-lcgamboa/pc-bios",
     "-drive", f"file={RUN}/flash.bin,if=mtd,format=raw", "-nographic", "-serial", f"file:{SERIAL}", "-monitor", "none",
     "-nic", "user,model=esp32_wifi,net=192.168.4.0/24,"
             "hostfwd=tcp::18080-192.168.4.1:80,hostfwd=tcp::18081-192.168.4.15:80",
     "-global", "driver=timer.esp32.timg,property=wdt_disable,value=true"],
    env=env, stdout=subprocess.DEVNULL, stderr=open(f"{RUN}/qemu.err", "w"))
ok = False
try:
    wait_for(lambda: "AP IP Address" in serial(), "access point", 120)
    log("firmware is in AP mode (no Wi-Fi configured)")
    got = wait_for(lambda: display() if display() and display()[0] == "XXXXXXXX" else None, "startup homing to finish", 300)
    log(f"startup finished: drums show [{got[0]}] (firmware's no-Wi-Fi signal) at emulated t={got[1]:.1f}s")
    status, body = http(18080, "/settings", {
        "wire1Addresses": "", "wire1Offsets": "",
        "mqtt_server": "192.168.4.2", "mqtt_port": 1883,
    })
    log(f"POST /settings -> {status} {body}")

    wait_for(lambda: "Reset reason: software reset" in serial(), "reboot", 240)
    log("rebooted (hardware settings changed); still in AP mode")
    wait_for(lambda: "Wire1" in serial().split("Restarting")[-1] and "Total modules" in serial().split("Restarting")[-1],
             "I2C config after reboot", 120)
    cfg = serial().split("Restarting")[-1]
    log("after reboot: " + " | ".join(l.strip() for l in cfg.splitlines() if l.startswith(("Wire", "Total"))))

    got = wait_for(lambda: display() if display() and display()[0] == "WIFI ERR" else None, "post-reboot homing", 300)
    log(f"post-reboot startup finished: drums show [{got[0]}] at emulated t={got[1]:.1f}s")
    status, body = http(18080, "/version")
    log(f"GET /version (AP mode) -> {body}")

    status, body = http(18080, "/text", {"mode": "single", "words": ["HELLO"], "center": True})
    log(f"POST /text HELLO -> {status} {body}")
    got = wait_for(lambda: display() if display() and display()[0] == " HELLO  " else None, "drums to show HELLO", 120)
    log(f"PASS web: drums show [{got[0]}] at emulated t={got[1]:.1f}s")

    # In AP mode the firmware only starts MQTT when a settings change asks it to reconnect.
    n = serial().count("[MQTT] Connected to broker")
    status, body = http(18080, "/settings", {"mqtt_user": "flapsim"})
    log(f"POST /settings mqtt_user (forces MQTT reconnect in AP mode) -> {status}")
    wait_for(lambda: serial().count("[MQTT] Connected to broker") > n, "MQTT connect", 120)
    log("MQTT: firmware connected to the host broker")
    time.sleep(3)
    before = display()
    subprocess.run(["mosquitto_pub", "-h", "127.0.0.1", "-t", "splitflap/splitflap/set", "-m", "WORLD"], check=True)
    log("published WORLD to splitflap/splitflap/set")
    wait_for(lambda: "[MQTT] Message received: WORLD" in serial(), "firmware to receive the MQTT message", 60)
    log("PASS mqtt: firmware received WORLD")
    # Known firmware limitation (findings doc, finding 3): in AP mode setDisplay() is never called, so the
    # message is dropped and the drums must NOT change. Assert that, so a firmware fix shows up as a change here.
    time.sleep(15)
    after = display()
    if after and before and after[0] != before[0]:
        raise SystemExit(f"UNEXPECTED: drums changed to [{after[0]}] in AP mode; finding 3 may be fixed, update this check")
    log(f"PASS mqtt (known limitation): drums unchanged at [{after[0] if after else '?'}] in AP mode")
    ok = True
finally:
    qemu.terminate()
    sub.terminate()
    sub.wait()  # mosquitto_sub buffers when writing to a file; its log is complete only after it exits
    broker.terminate()
    log("MQTT traffic seen on broker:\n    " + "\n    ".join(open(f"{RUN}/mqtt.log").read().strip().splitlines()))
    log("RESULT: " + ("PASS" if ok else "FAIL"))
