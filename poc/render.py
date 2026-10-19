"""Build a standalone replay page from a flapsim timeline (JSONL)."""
import json
import sys

timeline, out, title = sys.argv[1], sys.argv[2], sys.argv[3]
labels = {"motors off": "move finished"}
frames = []
with open(timeline) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        fr = json.loads(line)
        fr["label"] = labels.get(fr["event"], "moving" if fr["event"] == "tick" else fr["event"])
        frames.append(fr)

data = {"title": title, "steps": 2048, "magnet": 730, "frames": frames}
tpl = open(sys.argv[4] if len(sys.argv) > 4 else "viewer_template.html").read()
open(out, "w").write(tpl.replace("__DATA__", json.dumps(data, separators=(",", ":"))))
# Print indexes of interesting frames for screenshots.
for i, fr in enumerate(frames):
    if fr["event"] != "tick":
        print(i, fr["t"], fr["event"], "".join(m[3] for m in fr["modules"]))
print("total", len(frames))
