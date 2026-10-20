# POC (frozen reference)

This is the proof-of-concept code from October 2026. It is **not** the product. It is kept so the MVP can refer to
working behaviour. See [`../docs/poc-findings-and-mvp-plan.md`](../docs/poc-findings-and-mvp-plan.md) for results
and the plan.

## Reproduce (Linux, ~10 min)

```bash
# 1. Build deps
sudo apt-get install -y ninja-build libglib2.0-dev libgcrypt20-dev libslirp-dev libpixman-1-dev \
  python3-venv flex bison pkg-config bc mosquitto mosquitto-clients

# 2. QEMU fork at the POC commit, plus the hook patch and model
git clone -b picsimlab-esp32 https://github.com/lcgamboa/qemu qemu-lcgamboa
cd qemu-lcgamboa && git checkout e39508f79cb0681bbe15cfcebd40a3f45cdc3c48
git apply ../qemu-lcgamboa.patch
cp ../flapsim_model.c hw/xtensa/
flags="$(./configure --help | perl -ne 'print if s/^  ([a-z][\w-]*) .*/\1/' | tail -n +2 | awk '{print "--disable-"$1}' ORS=' ')"
./configure --target-list=xtensa-softmmu --extra-cflags=-fPIC $flags --enable-tcg --enable-system \
  --disable-werror --enable-gcrypt --enable-slirp
cd build && ninja qemu-system-xtensa && cd ../..

# 3. Firmware: v1.4.0 esp32_n4 factory image, padded to 4 MB
mkdir -p fw
curl -L -o fw/factory.bin \
  https://github.com/DrewFerg11/Split-Flap-Display/releases/download/v1.4.0/splitflap-esp32_n4-v1.4.0-factory.bin
python3 -c "d=open('fw/factory.bin','rb').read(); open('fw/esp32_n4-v1.4.0-flash4M.bin','wb').write(d+b'\xff'*(4*1024*1024-len(d)))"

# 4. Motion run: 16 modules, accurate timing; logs in runs/demo/
./experiment.sh demo 200 "20,21,22,23,24,25,26,27" -icount shift=3,sleep=off
python3 analyze.py runs/demo

# 5. Web scenario: AP mode -> settings -> reboot -> POST /text -> drums
python3 net_scenario.py net-demo

# 6. Replay page + screenshots (needs Node + Playwright for screenshots)
python3 render.py runs/demo/timeline.jsonl replay.html "v1.4.0 demo"
```

The scripts expect to be run from this folder, with `qemu-lcgamboa/` and `fw/` next to them.

## Files

| File | What it is |
|---|---|
| `qemu-lcgamboa.patch` | Points the fork's `picsimlab_i2c_event` hook at the model (expects `flapsim_model.c` in `hw/xtensa/`) |
| `flapsim_model.c` | PCF8575 + 28BYJ-48 + drum + hall model; configured by `FLAPSIM_*` env vars (see file header) |
| `experiment.sh` | Boot one configuration; outputs in `runs/<name>/` |
| `analyze.py` | Summarise a run |
| `net_scenario.py` | End-to-end HTTP scenario, plus an MQTT receive check (inbound display is a known AP-mode limitation) |
| `viewer_template.html`, `render.py`, `shot.js` | Replay page, renderer, screenshot helper |

Known POC limitations are listed in the main doc: env-var config, one board per process, and station-mode Wi-Fi
not working.
