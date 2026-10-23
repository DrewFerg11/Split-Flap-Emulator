"""Check structured telemetry while the existing real-firmware power probe runs."""

import collections
import json
import threading
import urllib.request

from websockets.sync.client import connect


class TelemetryCheck:
    def __init__(self, context):
        manifest = json.loads((context.out / "run.json").read_text())
        self.endpoint = manifest["control"]["endpoint"]
        self.token = (context.out / "control-token").read_text().strip()
        self.context = context
        self.types = collections.Counter()
        self.ends = collections.Counter()
        self.kinds = collections.Counter()
        self.gaps = collections.Counter()
        self.sources = set()
        self.cursor = None
        self.failure = None
        self.done = threading.Event()
        self.ws = connect(self.endpoint.replace("http:", "ws:") + "/api/emu/stream", proxy=None)
        self.ws.send(json.dumps({"token": self.token}))
        self.reader = threading.Thread(target=self.collect)
        self.reader.start()

    def collect(self):
        try:
            while not self.done.is_set():
                try:
                    page = json.loads(self.ws.recv(timeout=0.2))
                except TimeoutError:
                    continue
                if page["error"]:
                    raise RuntimeError(page["error"])
                if page["gap"]:
                    self.gaps[page["gap"]["reason"]] += 1
                for row in page["records"]:
                    self.types[row["type"]] += 1
                    if "source" in row:
                        self.sources.add(row["source"])
                    if row["type"] == "i2c":
                        self.ends[row["end"]] += 1
                        for event in row["events"]:
                            self.kinds[event["kind"]] += 1
                            if event["kind"] == 6 and "ack" in event:
                                raise RuntimeError("read byte misrepresented as an ACK")
                    elif row["type"] == "gap":
                        self.gaps[row["reason"]] += 1
                self.cursor = page["next"]
        except Exception as error:
            if not self.done.is_set():
                self.failure = error

    def finish(self):
        self.done.set()
        self.reader.join(2)
        self.ws.close()
        if self.reader.is_alive():
            raise RuntimeError("telemetry client did not stop")
        if self.failure:
            raise RuntimeError("telemetry stream failed") from self.failure
        if not all(self.types[k] for k in ["i2c", "state", "control", "lifecycle"]):
            raise RuntimeError(f"missing typed telemetry: {self.types}")
        if not all(self.kinds[k] for k in [0, 1, 3, 5, 6]):
            raise RuntimeError(f"missing actual firmware I2C reads/writes: {self.kinds}")
        if not self.cursor:
            raise RuntimeError("no stream cursor")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(
            self.endpoint + "/api/emu/i2c?limit=1&since=" + self.cursor,
            headers={"Authorization": "Bearer " + self.token},
        )
        with opener.open(request, timeout=3) as response:
            page = json.load(response)
        if len(page["records"]) > 1 or page["error"]:
            raise RuntimeError("HTTP resumption did not honor stream cursor/limit")
        result = {
            "complete": True,
            "types": dict(self.types),
            "ends": dict(self.ends),
            "raw_kinds": dict(self.kinds),
            "gaps": dict(self.gaps),
            "sources": len(self.sources),
            "cursor": self.cursor,
            "resumed_page": page,
        }
        (self.context.out / "telemetry-verification.json").write_text(
            json.dumps(result, indent=2) + "\n"
        )
