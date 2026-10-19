"""Summarize a flapsim run: move durations, step rate, wall vs emulated time."""
import json
import re
import sys

d = sys.argv[1]
frames = [json.loads(l) for l in open(f"{d}/timeline.jsonl") if l.strip()]
moves = [f for f in frames if f["event"] == "motors off"]
print(f"== {d}")
print(open(f"{d}/wall.txt").read().strip())
for f in moves:
    rows = {}
    for bus, addr, angle, ch, coil, hall in f["modules"]:
        rows.setdefault(bus, []).append(ch)
    print(f"  move finished t={f['t']:7.2f}s  " + " ".join("[" + "".join(r) + "]" for r in rows.values()))

# Step rate of bus0 0x20 while it is moving (sample-to-sample, forward steps only, excluding idle samples).
rates = []
for a, b in zip(frames, frames[1:]):
    da = (b["modules"][0][2] - a["modules"][0][2]) % 2048
    dt = b["t"] - a["t"]
    if 0 < da < 1000 and 0.05 < dt < 0.5:
        rates.append(da / dt)
if rates:
    rates.sort()
    print(f"  bus0 step rate while moving: median {rates[len(rates)//2]:.0f} steps/s (firmware target 512)")
last = frames[-1]["t"] if frames else 0
print(f"  last emulated timestamp: {last:.1f}s")
if frames and "wall" in frames[0]:
    w0 = frames[0]["wall"]
    for f in moves:
        print(f"    wall-clock for move at t={f['t']:.1f}s: {f['wall'] - w0:.1f}s after first snapshot")
    span_t = moves[-1]["t"] - frames[0]["t"] if moves else 0
    span_w = moves[-1]["wall"] - w0 if moves else 0
    if span_w:
        print(f"  speed: {span_t / span_w:.2f}x real time (emulated seconds per wall second, boot->last move)")
err = open(f"{d}/serial.log", errors="replace").read()
print("  serial errors:", sorted(set(re.findall(r"error code: (\d+)", err))))
