"""A supervised local Mosquitto broker and a minimal MQTT 3.1.1 observer for native scenarios.

The observer subscribes to `#` before the firmware boots, so it records every message the
device publishes (Home Assistant discovery, availability, state) and can publish commands.
QoS 0 only, which is what the firmware uses; no third-party Python dependency.
"""

import shutil
import socket
import struct
import subprocess
import threading
import time

from flapsim.process import stop_group


def _varint(value):
    out = bytearray()
    while True:
        byte, value = value % 128, value // 128
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _string(text):
    data = text.encode()
    return struct.pack("!H", len(data)) + data


def _packet(kind, body):
    return bytes([kind]) + _varint(len(body)) + body


class Broker:
    """mosquitto on 127.0.0.1:<port>, anonymous, no persistence, its own process group."""

    def __init__(self, out, port):
        executable = shutil.which("mosquitto")
        if executable is None:
            raise ValueError("the mqtt service needs the mosquitto broker on PATH")
        self.port = port
        config = out / "mosquitto.conf"
        config.write_text(
            f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n"
            f"log_dest file {out / 'mosquitto.log'}\nlog_type all\n"
        )
        self.command = [executable, "-c", str(config)]
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def wait_ready(self, context):
        while True:
            context.checkpoint()
            if self.process.poll() is not None:
                raise RuntimeError(f"mosquitto exited early ({self.process.returncode})")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError:
                context.pause()

    def stop(self):
        return stop_group(self.process)


class Observer:
    """Records every message on the broker; thread-safe reads for scenario steps."""

    def __init__(self, port, client_id="flapsim-observer"):
        self.messages = []
        self.lock = threading.Lock()
        self.socket = socket.create_connection(("127.0.0.1", port), timeout=5)
        connect = _string("MQTT") + bytes([4, 0x02]) + struct.pack("!H", 60) + _string(client_id)
        self.socket.sendall(_packet(0x10, connect))
        kind, body = self._read(idle_timeout=5)
        if kind >> 4 != 2 or len(body) != 2 or body[1] != 0:
            raise RuntimeError(f"MQTT observer was refused: {body!r}")
        self.socket.sendall(_packet(0x82, struct.pack("!H", 1) + _string("#") + b"\0"))
        kind, body = self._read(idle_timeout=5)
        if kind >> 4 != 9 or body[-1] == 0x80:
            raise RuntimeError("MQTT observer could not subscribe to #")
        self.closed = False
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _exact(self, count):
        data = b""
        while len(data) < count:
            chunk = self.socket.recv(count - len(data))
            if not chunk:
                raise ConnectionError("MQTT broker closed the observer connection")
            data += chunk
        return data

    def _read(self, idle_timeout=None):
        """One packet. Only the wait for its first byte may time out, never a partial packet."""
        self.socket.settimeout(idle_timeout)
        kind = self._exact(1)[0]
        self.socket.settimeout(5)
        length, shift = 0, 0
        while True:
            byte = self._exact(1)[0]
            length += (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        return kind, self._exact(length)

    def _run(self):
        last_ping = time.monotonic()
        while not self.closed:
            try:
                kind, body = self._read(idle_timeout=1)
            except TimeoutError:
                if time.monotonic() - last_ping > 30:
                    self.socket.sendall(b"\xc0\0")
                    last_ping = time.monotonic()
                continue
            except OSError as error:
                if not self.closed:
                    self.error = str(error)
                return
            if kind >> 4 != 3:
                continue
            qos = (kind >> 1) & 3
            length = struct.unpack("!H", body[:2])[0]
            topic = body[2 : 2 + length].decode(errors="replace")
            payload = body[2 + length + (2 if qos else 0) :]
            with self.lock:
                self.messages.append(
                    {
                        "index": len(self.messages),
                        "topic": topic,
                        "payload": payload.decode(errors="replace"),
                        "retained": bool(kind & 1),
                        "monotonic": time.monotonic(),
                    }
                )

    def snapshot(self):
        with self.lock:
            return list(self.messages)

    def publish(self, topic, payload, retain=False):
        if self.error:
            raise RuntimeError(f"MQTT observer failed: {self.error}")
        body = _string(topic) + payload.encode()
        self.socket.sendall(_packet(0x30 | (1 if retain else 0), body))

    def close(self):
        self.closed = True
        try:
            self.socket.sendall(b"\xe0\0")
        except OSError:
            pass
        self.socket.close()
        self.thread.join(2)
