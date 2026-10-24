"""Bounded I2C history, independent collection and bounded rotating evidence."""

import collections
import contextlib
import json
import os
import socket
import struct
import threading

from flapsim.state import validate_state

MAGIC = 0x464C415049324331
HEADER = struct.Struct("=14Q")
EVENT = struct.Struct("=5Q")
MAX_PACKET = HEADER.size + 65536
MAX_RECORDS = 4096
MAX_BYTES = 4 * 1024 * 1024
MAX_LIMIT = 128
KINDS = {1: "source_started", 2: "reset", 3: "power_off", 4: "source_ended"}
ENDS = {
    0: "fragment",
    1: "stop",
    2: "replaced_start",
    3: "reset",
    4: "power_off",
    5: "source_end",
    6: "orphan",
}


class CursorError(ValueError):
    def __init__(self, code, status=422):
        super().__init__(code)
        self.status = status


class History:
    def __init__(self, run_id, *, max_records=MAX_RECORDS, max_bytes=MAX_BYTES):
        self.run_id = run_id
        self.max_records, self.max_bytes = max_records, max_bytes
        self.condition = threading.Condition()
        self.records = collections.deque()
        self.sequence = self.bytes = self.evicted = 0
        self.closed = False
        self.error = None

    def cursor(self, sequence):
        return f"{self.run_id}:{sequence}"

    def parse_cursor(self, value):
        if value is None:
            return 0
        if not isinstance(value, str) or len(value) > 80:
            raise CursorError("invalid_cursor")
        prefix, separator, number = value.partition(":")
        if not separator or prefix != self.run_id:
            raise CursorError("foreign_run_cursor", 409)
        if not number.isascii() or not number.isdecimal() or str(int(number)) != number:
            raise CursorError("invalid_cursor")
        sequence = int(number)
        if sequence > 2**64 - 1:
            raise CursorError("invalid_cursor")
        return sequence

    def publish(self, record):
        with self.condition:
            if self.closed:
                return
            if self.sequence == 2**64 - 1:
                raise RuntimeError("telemetry cursor exhausted")
            self.sequence += 1
            value = {
                "schema_version": 1,
                "run_id": self.run_id,
                "cursor": self.cursor(self.sequence),
                **record,
            }
            encoded = (json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n").encode()
            self.records.append((self.sequence, value, encoded))
            self.bytes += len(encoded)
            while len(self.records) > self.max_records or self.bytes > self.max_bytes:
                _, _, old = self.records.popleft()
                self.bytes -= len(old)
                self.evicted += 1
            self.condition.notify_all()

    def _slice(self, sequence, limit):
        if sequence > self.sequence:
            raise CursorError("future_cursor", 409)
        first = self.records[0][0] if self.records else self.sequence + 1
        gap = None
        if sequence < first - 1:
            gap = {
                "reason": "retention",
                "after": self.cursor(sequence),
                "through": self.cursor(first - 1),
                "records": str(first - 1 - sequence),
            }
            sequence = first - 1
        selected = [r for r in self.records if r[0] > sequence][:limit]
        next_sequence = selected[-1][0] if selected else sequence
        return selected, gap, next_sequence

    def page(self, since=None, limit=MAX_LIMIT):
        if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
            raise CursorError("invalid_limit")
        sequence = self.parse_cursor(since)
        with self.condition:
            records, gap, next_sequence = self._slice(sequence, limit)
            return {
                "api_version": 1,
                "run_id": self.run_id,
                "records": [r[1] for r in records],
                "next": self.cursor(next_sequence),
                "latest": self.cursor(self.sequence),
                "oldest": self.cursor(self.records[0][0]) if self.records else None,
                "gap": gap,
                "more": next_sequence < self.sequence,
                "stopped": self.closed,
                "error": self.error,
            }

    def fail(self, error):
        with self.condition:
            self.error = f"{type(error).__name__}: {error}"[:160]
            self.condition.notify_all()

    def stop(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()


class Collector:
    def __init__(self, out, run_id, hardware, *, journal_bytes=MAX_BYTES, **history_options):
        self.out, self.hardware = out, hardware
        self.history = History(run_id, **history_options)
        self.journal_bytes = journal_bytes
        self.journal_cursor = 0
        self.rotations = 0
        self.source = None
        self.packet = 0
        self.ended = True
        self.sources = self.dropped_packets = self.dropped_events = 0
        self._source_drops = self._source_event_drops = 0
        self.reader = self.connection = None
        self.writer = threading.Thread(target=self._journal, daemon=True)
        self.writer.start()

    def _journal(self):
        path = self.out / "i2c.jsonl"
        log = None
        try:
            log = path.open("ab")
            while True:
                with self.history.condition:
                    rows, gap, cursor = self.history._slice(self.journal_cursor, MAX_LIMIT)
                    if not rows and not gap:
                        if self.history.closed:
                            break
                        self.history.condition.wait(0.1)
                        continue
                encoded = [r[2] for r in rows]
                if gap:
                    encoded.insert(
                        0,
                        (
                            json.dumps(
                                {
                                    "schema_version": 1,
                                    "run_id": self.history.run_id,
                                    "type": "gap",
                                    **gap,
                                    "reason": "journal_retention",
                                },
                                separators=(",", ":"),
                            )
                            + "\n"
                        ).encode(),
                    )
                for data in encoded:
                    if log.tell() and log.tell() + len(data) > self.journal_bytes:
                        log.flush()
                        os.fsync(log.fileno())
                        log.close()
                        log = None
                        os.replace(path, self.out / "i2c.previous.jsonl")
                        self.rotations += 1
                        log = path.open("wb")
                    log.write(data)
                log.flush()
                self.journal_cursor = cursor
            log.flush()
            os.fsync(log.fileno())
        except (OSError, ValueError) as error:
            self.history.fail(error)
        finally:
            if log is not None:
                log.close()

    def receive(self, data):
        if len(data) < HEADER.size:
            raise ValueError("short telemetry header")
        (
            magic,
            source,
            sequence,
            transaction,
            generation,
            bus,
            addr,
            kind,
            end,
            count,
            guest,
            drops,
            dropped_events,
            raw_sequence,
        ) = HEADER.unpack_from(data)
        if magic != MAGIC or sequence == 0 or guest >= 2**63:
            raise ValueError("invalid telemetry identity")
        source = f"{source:016x}"
        if source != self.source:
            if not self.ended:
                self.history.publish(
                    {
                        "type": "gap",
                        "reason": "source_tail_unknown",
                        "source": self.source,
                        "exact_count": None,
                    }
                )
            self.source, self.packet, self.ended = source, 0, False
            self._source_drops = self._source_event_drops = 0
            self.sources += 1
        if (
            sequence <= self.packet
            or drops < self._source_drops
            or dropped_events < self._source_event_drops
        ):
            raise ValueError("telemetry producer sequence moved backwards")
        if sequence != self.packet + 1:
            self.history.publish(
                {
                    "type": "gap",
                    "reason": "producer_loss",
                    "source": source,
                    "after_packet": str(self.packet),
                    "through_packet": str(sequence - 1),
                    "packets": str(sequence - self.packet - 1),
                    "dropped_events": str(dropped_events - self._source_event_drops),
                }
            )
        self.dropped_packets += drops - self._source_drops
        self.dropped_events += dropped_events - self._source_event_drops
        self._source_drops, self._source_event_drops = drops, dropped_events
        self.packet = sequence
        common = {
            "source": source,
            "producer_sequence": str(sequence),
            "guest_ns": str(guest),
            "boot_generation": str(generation),
            "raw_sequence": str(raw_sequence),
        }
        payload = data[HEADER.size :]
        if kind == 0:
            if (
                bus > 255
                or addr > 255
                or end not in ENDS
                or count > 8
                or len(payload) != count * EVENT.size
            ):
                raise ValueError("invalid transaction fragment")
            events = []
            for offset in range(0, len(payload), EVENT.size):
                seq, when, address, raw, result = EVENT.unpack_from(payload, offset)
                if (
                    not 1 <= seq <= raw_sequence
                    or when >= 2**63
                    or address > 255
                    or raw > 65535
                    or result > 255
                ):
                    raise ValueError("invalid I2C event")
                event = {
                    "sequence": str(seq),
                    "guest_ns": str(when),
                    "addr": address,
                    "event": raw,
                    "kind": raw & 255,
                    "data": raw >> 8,
                    "result": result,
                }
                if event["kind"] <= 2 or event["kind"] == 5:
                    event["ack"] = result == 0
                events.append(event)
            self.history.publish(
                {
                    **common,
                    "type": "i2c",
                    "bus": bus,
                    "addr": addr,
                    "transaction": str(transaction) if transaction else None,
                    "end": ENDS[end],
                    "events": events,
                }
            )
        elif kind == 5:
            if count != len(payload) or count > 65536:
                raise ValueError("invalid telemetry snapshot size")
            state = json.loads(payload)
            validate_state(state, self.hardware, require_settled=False)
            self.history.publish({**common, "type": "state", "state": state})
        elif kind in KINDS and not payload and count == 0:
            self.history.publish({**common, "type": "lifecycle", "event": KINDS[kind]})
            self.ended = kind == 4
        else:
            raise ValueError("unknown telemetry packet")

    @contextlib.contextmanager
    def channel(self):
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.connection = parent

        def read():
            try:
                while True:
                    data, _, flags, _ = parent.recvmsg(MAX_PACKET)
                    if not data:
                        break
                    if flags & socket.MSG_TRUNC:
                        raise ValueError("oversized telemetry packet")
                    self.receive(data)
            except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
                self.history.fail(error)

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        try:
            yield {"FLAPSIM_I2C_FD": str(child.fileno())}, (child.fileno(),)
        finally:
            child.close()
            self.reader.join(2)
            if self.reader.is_alive():
                parent.shutdown(socket.SHUT_RD)
                self.reader.join(1)
                self.history.fail(RuntimeError("telemetry transport did not close"))
            parent.close()
            self.connection = None
            if not self.ended:
                self.history.publish(
                    {
                        "type": "gap",
                        "reason": "source_tail_unknown",
                        "source": self.source,
                        "exact_count": None,
                    }
                )
                self.ended = True

    def audit(self, entry):
        result = entry.get("result", {})
        self.history.publish(
            {
                "type": "control",
                "phase": entry["phase"],
                "command": entry["command"],
                "result": {
                    k: result[k]
                    for k in (
                        "status",
                        "error",
                        "id",
                        "boot_generation",
                        "sequence",
                        "guest_ns",
                        "mechanics_revision",
                        "operation",
                    )
                    if k in result
                },
            }
        )

    def metadata(self):
        return {
            "enabled": True,
            "format_version": 1,
            "status": "failed"
            if self.history.error
            else "stopped"
            if self.history.closed
            else "running",
            "error": self.history.error,
            "records_retained": len(self.history.records),
            "records_evicted": str(self.history.evicted),
            "max_records": self.history.max_records,
            "max_bytes": self.history.max_bytes,
            "sources": self.sources,
            "producer_dropped_packets": str(self.dropped_packets),
            "producer_dropped_events": str(self.dropped_events),
            "journal": {
                "files": ["i2c.previous.jsonl", "i2c.jsonl"],
                "bytes_per_file": self.journal_bytes,
                "rotations": self.rotations,
                "through": self.history.cursor(self.journal_cursor),
            },
        }

    def close(self):
        if self.history.closed:
            return
        self.history.publish({"type": "lifecycle", "event": "stream_stopped"})
        self.history.stop()
        self.writer.join(2)
        if self.writer.is_alive():
            self.history.fail(RuntimeError("telemetry journal flush timed out"))
        if self.history.error:
            raise RuntimeError("structured telemetry failed: " + self.history.error)
