#!/bin/bash
# Usage: experiment.sh <name> <wall-timeout-s> <bus1 list or ""> [extra qemu args...]
# Runs v1.4.0 esp32_n4 under flapsim and records timeline + logs in runs/<name>/.
set -u
P=$(cd "$(dirname "$0")" && pwd)
name=$1; tmo=$2; bus1=$3; shift 3
d=$P/runs/$name
mkdir -p "$d"
cp "$P/fw/esp32_n4-v1.4.0-flash4M.bin" "$d/flash.bin"
start=$(date +%s.%N)
FLAPSIM_BUS1=$bus1 FLAPSIM_SEED=7 FLAPSIM_TIMELINE=$d/timeline.jsonl \
  timeout "$tmo" "$P/qemu-lcgamboa/build/qemu-system-xtensa" -M esp32-picsimlab "$@" \
  -L "$P/qemu-lcgamboa/pc-bios" -drive file="$d/flash.bin",if=mtd,format=raw -nographic \
  -serial file:"$d/serial.log" -monitor none -nic user,model=esp32_wifi,net=192.168.4.0/24 \
  -global driver=timer.esp32.timg,property=wdt_disable,value=true > /dev/null 2> "$d/qemu.err"
end=$(date +%s.%N)
echo "wall=$(echo "$end - $start" | bc)" > "$d/wall.txt"
