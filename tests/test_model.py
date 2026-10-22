"""Drive the real C model directly, without a QEMU build or firmware logic."""

import ctypes
import json
import os
import shlex
import socket
import subprocess
from pathlib import Path

import pytest

from flapsim.hardware import normalize_hardware, write_model_config
from flapsim.state import read_settlement, validate_state

ROOT = Path(__file__).resolve().parents[1]
CORE = [
    "bus",
    "config",
    "control",
    "drum",
    "faults",
    "io",
    "mechanics",
    "pcf8575",
    "resume",
    "stepper",
]
COILS = [0xFFE7, 0xFFF3, 0xFFF9, 0xFFED]
OFF = 0xFFE1


@pytest.fixture(scope="session")
def model_library(tmp_path_factory):
    output = tmp_path_factory.mktemp("native-model") / "model.so"
    flags = subprocess.run(
        ["pkg-config", "--cflags", "--libs", "json-c"], capture_output=True, text=True, check=True
    ).stdout
    subprocess.run(
        [
            *shlex.split(os.environ.get("CC", "cc")),
            "-std=c11",
            "-O1",
            "-g",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-fPIC",
            "-shared",
            *shlex.split(os.environ.get("CFLAGS", "")),
            *[str(ROOT / f"model/{name}.c") for name in CORE],
            *shlex.split(flags),
            *shlex.split(os.environ.get("LDFLAGS", "")),
            "-o",
            str(output),
        ],
        check=True,
    )
    lib = ctypes.CDLL(str(output))
    lib.fs_model_init.argtypes = [ctypes.c_char_p]
    lib.fs_model_init.restype = ctypes.c_int
    lib.fs_model_event.argtypes = [ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint16, ctypes.c_int64]
    lib.fs_model_event.restype = ctypes.c_int
    lib.fs_model_resume.argtypes = [ctypes.c_char_p, ctypes.c_int64]
    lib.fs_model_resume.restype = ctypes.c_int
    lib.fs_model_tick.argtypes = [ctypes.c_int64]
    lib.fs_model_error.restype = ctypes.c_char_p
    lib.fs_control_serve.argtypes = [ctypes.c_int, ctypes.c_int64]
    lib.fs_control_serve.restype = ctypes.c_int
    return lib


class Model:
    def __init__(
        self,
        library,
        out,
        *,
        angles=(0,),
        buses=(0,),
        steps=2048,
        magnet=730,
        window=40,
        flaps=" ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        flap_offset=0,
        polarity="active-high",
        module_overrides=None,
    ):
        self.lib, self.out, self.now = library, out, 0
        modules = [
            {"id": b, "modules": [32 + i for i, bus in enumerate(buses) if bus == b]}
            for b in sorted(set(buses))
        ]
        if module_overrides:
            for bus in modules:
                bus["modules"] = [
                    {"addr": addr, **module_overrides.get(addr - 32, {})} for addr in bus["modules"]
                ]
        # One address per supplied module; tests use [0,1] for the slow-row regression.
        document = {
            "chip": "esp32",
            "buses": modules,
            "drum": {
                "steps_per_rotation": steps,
                "magnet_position": magnet,
                "sensor_window": window,
                "flaps": flaps,
                "flap_offset": flap_offset,
                "sensor_polarity": polarity,
            },
            "start_angles": list(angles),
        }
        self.config = write_model_config(normalize_hardware(document), out)
        assert library.fs_model_init(str(self.config).encode()) == 0, library.fs_model_error()

    def event(self, kind, bus=0, addr=32):
        self.now += 1_000_000
        return self.lib.fs_model_event(bus, addr, kind, self.now)

    def write(self, value, bus=0, addr=32):
        assert self.event(1, bus, addr) == 0
        self.event(((value & 255) << 8) | 5, bus, addr)
        self.event(((value >> 8) << 8) | 5, bus, addr)
        self.event(3, bus, addr)

    def read(self, bus=0, addr=32):
        self.event(0, bus, addr)
        low, high = self.event(6, bus, addr), self.event(6, bus, addr)
        self.event(4, bus, addr)
        self.event(3, bus, addr)
        return low | (high << 8)

    def tick(self, advance=100_000_000):
        self.now += advance
        self.lib.fs_model_tick(self.now)
        assert not self.lib.fs_model_error(), self.lib.fs_model_error()

    def control(self, patch, *, identifier=1, revision=0, bus=0, addr=32):
        command = {
            "id": identifier,
            "expected_revision": revision,
            "bus": bus,
            "addr": addr,
            "patch": patch,
        }
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with parent, child:
            parent.sendall(b"FLAPSIM_MECH/1 " + json.dumps(command).encode())
            assert self.lib.fs_control_serve(child.fileno(), self.now) == 1
            return json.loads(parent.recv(65536))

    def state(self):
        return json.loads((self.out / "state.json").read_text())

    def activity(self):
        return json.loads((self.out / "activity.json").read_text())

    def timeline(self):
        return [json.loads(line) for line in (self.out / "timeline.jsonl").read_text().splitlines()]


@pytest.fixture
def make_model(model_library, tmp_path):
    def create(**kwargs):
        return Model(model_library, tmp_path, **kwargs)

    yield create
    model_library.fs_model_close()


def test_first_energize_forward_duplicate_and_stop_restart(make_model):
    model = make_model()
    model.write(COILS[0])
    model.write(COILS[0])
    model.write(COILS[1])
    model.write(COILS[2])
    model.write(OFF)
    first = model.state()["modules"][0]
    assert first["angle"] == 2
    assert first["counters"]["forward"] == 2
    assert first["counters"]["stalls"] == 0
    model.write(COILS[3])
    model.write(OFF)
    assert model.state()["modules"][0]["angle"] == 2
    assert model.state()["movement_id"] == 2


def test_reverse_wrap_and_opposite_phase_stall(make_model):
    model = make_model()
    model.write(COILS[0])
    model.write(COILS[3])
    model.write(COILS[0])
    model.write(COILS[2])
    model.write(OFF)
    module = model.state()["modules"][0]
    assert module["angle"] == 0
    assert module["counters"] == {
        "forward": 1,
        "backward": 1,
        "stalls": 1,
        "writes": 5,
        "reads": 0,
        "anomalies": 0,
    }


def test_latch_byte_order_and_readback(make_model):
    model = make_model()
    assert model.read() == 0x7FE1
    model.event(1)
    model.event(0xE705)
    assert model.read() == 0x7FE1  # Interrupted one-byte write never commits.
    model.write(COILS[0])
    assert model.read() == 0x7FE7
    model.write(OFF)
    assert model.state()["counters"]["reads"] == 3
    assert model.state()["counters"]["anomalies"] == 1


@pytest.mark.parametrize(
    "angle,hall", [(0, True), (1, True), (2, False), (11, False), (12, True), (15, True)]
)
def test_hall_window_wrap_and_boundaries(make_model, angle, hall):
    model = make_model(steps=16, magnet=12, window=6, flaps=" AB", angles=(angle,))
    assert bool(model.read() & 0x8000) == hall


@pytest.mark.parametrize(
    "angle,shown", [(0, " "), (1, " "), (2, "A"), (5, "A"), (6, "B"), (14, " "), (15, " ")]
)
def test_nearest_flap_and_ties(make_model, angle, shown):
    model = make_model(steps=16, magnet=0, window=1, flaps=" ABC", angles=(angle,))
    assert model.state()["text"] == shown


@pytest.mark.parametrize("length", [1, 3, 4])
def test_malformed_writes_do_not_commit(make_model, length):
    model = make_model()
    model.event(1)
    for byte in [0xE7, 0xFF, 0x42, 0x12][:length]:
        model.event((byte << 8) | 5)
    model.event(3)
    model.tick()
    assert model.activity()["counters"]["anomalies"] == 1
    assert model.activity()["counters"]["writes"] == 0
    assert model.state()["modules"][0]["latch"] == OFF
    assert not any(entry["event"] == "move_started" for entry in model.timeline())


def test_absent_addresses_nack_without_spurious_anomalies(make_model):
    model = make_model()
    for bus, address in [(0, 33), (1, 32), (1, 39)]:
        assert model.event(1, bus, address) != 0
        model.event(3, bus, address)
    model.tick()
    assert model.activity()["counters"]["anomalies"] == 0


def test_all_modules_must_stop_and_old_state_is_stale(make_model):
    model = make_model(angles=(0, 0), buses=(0, 1))
    old = model.state()
    model.write(COILS[0], bus=0, addr=32)
    model.write(COILS[0], bus=1, addr=33)
    model.write(OFF, bus=0, addr=32)
    assert model.state() == old
    active = model.activity()
    assert not active["settled"]
    assert active["movement_id"] > old["movement_id"]
    assert not any(e["event"] == "move_finished" for e in model.timeline())
    model.write(OFF, bus=1, addr=33)
    final = model.state()
    assert final["settled"] and final["movement_id"] == active["movement_id"]
    assert final["settlement_id"] == 1
    assert all(not m["energized"] for m in final["modules"])
    assert model.activity()["settlement_id"] == final["settlement_id"]
    assert not list(model.out.glob("*.tmp"))


def test_guest_time_snapshots_while_energized(make_model):
    model = make_model()
    model.write(COILS[0])
    for _ in range(3):
        model.tick()
    snapshots = [e for e in model.timeline() if e["event"] == "snapshot"]
    assert len(snapshots) == 3
    assert all(
        b["guest_ns"] - a["guest_ns"] == 100_000_000
        for a, b in zip(snapshots, snapshots[1:], strict=False)
    )
    assert all(not e["settled"] for e in snapshots)
    model.write(OFF)
    state = model.state()
    model.tick()
    assert model.state() == state
    assert model.activity()["guest_ns"] > state["guest_ns"]


def test_escaped_flap_characters_form_valid_json(make_model):
    model = make_model(steps=16, magnet=0, window=1, flaps=' "\\', angles=(5,))
    assert model.state()["text"] == '"'
    assert model.timeline()[0]["text"] == '"'


def test_bad_normalized_config_fails_without_crashing(model_library, tmp_path):
    config = tmp_path / "bad.json"
    config.write_text('{"schema_version": 99, "chip": "esp32", "drum": false}')
    assert model_library.fs_model_init(str(config).encode()) != 0
    assert model_library.fs_model_error()
    model_library.fs_model_close()


def test_writes_commit_only_on_finish(make_model):
    model = make_model()
    model.event(1)
    model.event(0xE705)
    model.event(0xFF05)
    model.tick()
    assert model.activity()["settled"]
    assert model.activity()["counters"]["writes"] == 0
    model.event(3)
    assert not model.activity()["settled"]
    model.write(OFF)
    assert model.state()["counters"]["writes"] == 2


@pytest.mark.parametrize("count", [1, 3])
def test_malformed_reads_are_counted(make_model, count):
    model = make_model()
    model.event(0)
    for _ in range(count):
        model.event(6)
    model.event(3)
    model.tick()
    assert model.activity()["counters"]["anomalies"] == 1
    assert model.activity()["counters"]["reads"] == 0


def test_guest_reset_keeps_drum_latch_phase_and_counters(make_model):
    model = make_model()
    model.write(COILS[0])
    model.write(COILS[1])
    model.event(1)
    model.event(0xE705)  # A guest reset aborts this unfinished transaction.
    model.lib.fs_model_guest_reset()
    assert model.read() == 0x7FF3
    model.write(COILS[2])
    model.write(OFF)
    state = model.state()
    assert state["modules"][0]["angle"] == 2
    assert state["counters"]["forward"] == 2
    assert state["counters"]["anomalies"] == 0
    reset = next(e for e in model.timeline() if e["event"] == "guest_reset")
    assert reset["modules"][0]["angle"] == 1
    assert reset["modules"][0]["latch"] == COILS[1]
    assert reset["modules"][0]["energized"]
    assert model.lib.fs_model_init(str(model.config).encode()) == 0
    assert model.state()["modules"][0]["angle"] == 0
    assert model.state()["movement_id"] == 0


def test_invalid_coils_hold_energy_until_stopped(make_model):
    model = make_model()
    model.write(0xFFFF)
    model.tick()
    assert not model.activity()["settled"]
    model.write(OFF)
    assert model.state()["counters"]["anomalies"] == 1
    for entry in model.timeline():
        assert entry["settled"] == all(not m["energized"] for m in entry["modules"])


def test_settlement_reader_rejects_previous_movement(make_model):
    model = make_model()
    hardware = json.loads(model.config.read_text())
    assert read_settlement(model.out, hardware)["movement_id"] == 0
    model.write(COILS[0])
    assert read_settlement(model.out, hardware) is None
    model.write(OFF)
    assert read_settlement(model.out, hardware)["movement_id"] == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_module",
        "energized",
        "wrong_bus",
        "bad_char",
        "wrong_flap",
        "bad_angle",
        "bad_hall",
        "bad_latch",
        "bad_phase",
        "bad_text",
        "bad_rows",
        "bad_counter",
        "mismatched_total",
        "bool_schema",
    ],
)
def test_settlement_reader_rejects_incomplete_or_invalid_data(make_model, mutation):
    model = make_model()
    hardware = json.loads(model.config.read_text())
    state = model.state()
    module = state["modules"][0]
    if mutation == "missing_module":
        state["modules"] = []
    elif mutation == "energized":
        module["energized"] = True
    elif mutation == "wrong_bus":
        module["bus"] = False
    elif mutation == "bad_char":
        module["char"] = "XX"
    elif mutation == "wrong_flap":
        module["char"] = "X"
        state["text"] = "X"
        state["rows"] = ["X", ""]
    elif mutation == "bad_angle":
        module["angle"] = -1
    elif mutation == "bad_hall":
        module["hall"] = 1
    elif mutation == "bad_latch":
        module["latch"] = 65536
    elif mutation == "bad_phase":
        module["phase"] = 4
    elif mutation == "bad_text":
        state["text"] = "X"
    elif mutation == "bad_rows":
        state["rows"] = []
    elif mutation == "bad_counter":
        module["counters"]["reads"] = True
    elif mutation == "mismatched_total":
        state["counters"]["reads"] = 1
    elif mutation == "bool_schema":
        state["schema_version"] = True
    (model.out / "state.json").write_text(json.dumps(state))
    with pytest.raises(ValueError):
        read_settlement(model.out, hardware)


def test_activity_race_during_state_read_is_not_accepted(make_model, monkeypatch):
    model = make_model()
    hardware = json.loads(model.config.read_text())
    before = model.activity()
    after = {**before, "movement_id": 1, "settled": False}
    values = iter([json.dumps(before), json.dumps(after)])
    original = Path.read_text

    def changed_read(path, *args, **kwargs):
        return next(values) if path.name == "activity.json" else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", changed_read)
    assert read_settlement(model.out, hardware) is None


@pytest.mark.parametrize("angle,position,error", [(0, 3.5, -0.5), (2, 0, 0), (3, 0.25, 0.25)])
def test_fractional_flaps_include_offset_ties_and_wrap(make_model, angle, position, error):
    model = make_model(steps=16, magnet=0, window=1, flaps=" ABC", angles=(angle,), flap_offset=2)
    module = model.state()["modules"][0]
    assert module["flap_position"] == position
    assert module["flap_error"] == error
    assert model.timeline()[0]["modules"][0]["flap_position"] == position
    assert read_settlement(model.out, json.loads(model.config.read_text()))


def test_each_module_uses_its_own_rotation_and_magnet(make_model):
    model = make_model(
        steps={"mean": 16, "spread": 2},
        magnet={"mean": 2, "spread": 2},
        flaps=" AB",
        window=1,
        angles=(0, 0),
        buses=(0, 1),
    )
    config = json.loads(model.config.read_text())
    assert config["modules"][0]["steps_per_rotation"] != config["modules"][1]["steps_per_rotation"]
    for i, module in enumerate(config["modules"]):
        # One true revolution wraps exactly even though the two modules differ.
        bus, addr = module["bus"], module["addr"]
        model.write(COILS[0], bus=bus, addr=addr)
        for step in range(1, module["steps_per_rotation"] + 1):
            model.write(COILS[step % 4], bus=bus, addr=addr)
            assert bool(model.read(bus=bus, addr=addr) & 0x8000) == (
                step % module["steps_per_rotation"] == module["magnet_position"]
            )
        model.write(OFF, bus=bus, addr=addr)
        assert model.state()["modules"][i]["angle"] == 0


@pytest.mark.parametrize(
    "key,value", [("flap_position", 1), ("flap_error", float("nan")), ("flap_error", True)]
)
def test_settlement_rejects_forged_fractional_position(make_model, key, value):
    model = make_model()
    state = model.state()
    state["modules"][0][key] = value
    (model.out / "state.json").write_text(json.dumps(state))
    with pytest.raises(ValueError, match=key):
        read_settlement(model.out, json.loads(model.config.read_text()))


def test_legacy_v1_normalized_geometry_still_loads(make_model):
    model = make_model(angles=(730,))
    config = json.loads(model.config.read_text())
    for module in config["modules"]:
        for key in ["steps_per_rotation", "magnet_position", "flap_offset"]:
            del module[key]
    model.config.write_text(json.dumps(config))
    assert model.lib.fs_model_init(str(model.config).encode()) == 0
    assert model.state()["modules"][0]["hall"] is True
    assert read_settlement(model.out, config)


def test_mixed_v1_config_inherits_global_flap_offset_like_python_reader(make_model):
    model = make_model(angles=(730,), flap_offset=3)
    config = json.loads(model.config.read_text())
    del config["modules"][0]["flap_offset"]
    model.config.write_text(json.dumps(config))
    assert model.lib.fs_model_init(str(model.config).encode()) == 0
    state = read_settlement(model.out, config)
    assert state["modules"][0]["flap_position"] == pytest.approx((730 - 3) * 37 / 2048)


def test_firmware_assumed_rotation_does_not_reset_physical_drum(make_model):
    model = make_model(steps=2038)
    model.write(COILS[0])
    for step in range(1, 2049):
        model.write(COILS[step % 4])
    model.write(OFF)
    module = model.state()["modules"][0]
    assert module["angle"] == 10
    assert module["counters"]["forward"] == 2048
    assert module["flap_error"] == pytest.approx(10 * 37 / 2038)


@pytest.mark.parametrize(
    "key,value",
    [
        ("steps_per_rotation", 0),
        ("magnet_position", 2048),
        ("flap_offset", -1),
        ("steps_per_rotation", True),
    ],
)
def test_bad_normalized_module_geometry_fails(make_model, key, value):
    model = make_model()
    config = json.loads(model.config.read_text())
    config["modules"][0][key] = value
    model.config.write_text(json.dumps(config))
    assert model.lib.fs_model_init(str(model.config).encode()) != 0
    assert model.lib.fs_model_error()


def resume(model, guest_ns):
    model.tick(0)
    model.lib.fs_model_guest_reset()
    model.lib.fs_model_close()
    result = model.lib.fs_model_resume(str(model.config).encode(), guest_ns)
    model.now = guest_ns
    return result


def test_native_resume_keeps_drums_latches_ids_and_appends(make_model):
    model = make_model(angles=(100, 200), buses=(0, 0))
    for coils in COILS[:3]:
        model.write(coils)
    before = model.timeline()
    last = before[-1]
    guest = model.now + 5_000_000
    assert resume(model, guest) == 0, model.lib.fs_model_error()
    after = model.timeline()
    assert after[: len(before)] == before
    # Exactly one reset record, written before the restart with the live (not stale) state.
    resets = [r for r in after if r["event"] == "guest_reset"]
    assert len(resets) == 1 and after[-1] == resets[0]
    reset = resets[0]
    assert reset["modules"][0]["angle"] == 102 and reset["modules"][0]["energized"]
    assert reset["sequence"] == last["sequence"] + 1
    # Aggregates are re-summed from the restored per-module counters (only anomalies are kept).
    assert reset["counters"] == {
        key: sum(m["counters"][key] for m in reset["modules"]) for key in reset["counters"]
    }
    assert reset["counters"]["forward"] == 2 and reset["counters"]["writes"] == 3
    assert (reset["movement_id"], reset["settlement_id"]) == (
        last["movement_id"],
        last["settlement_id"],
    )
    assert model.activity()["guest_ns"] == guest
    # The motor keeps stepping from where it was: no invented step, no stall.
    model.write(COILS[3])
    model.write(OFF)
    final = model.state()
    assert final["modules"][0]["angle"] == 103 and final["counters"]["stalls"] == 0
    assert final["settlement_id"] == last["settlement_id"] + 1


def test_native_resume_discards_a_torn_final_line(make_model):
    model = make_model()
    model.write(COILS[0])
    complete = model.timeline()
    model.lib.fs_model_close()
    with (model.out / "timeline.jsonl").open("a") as timeline:
        timeline.write('{"schema_version": 1, "sequ')
    assert model.lib.fs_model_resume(str(model.config).encode(), model.now) == 0
    resumed = model.timeline()
    # A crash records no reset: resume discards the torn line and records it instead.
    assert resumed[:-1] == complete and resumed[-1]["event"] == "guest_reset"


@pytest.mark.parametrize("problem", ["backwards", "other_hardware", "empty", "forged"])
def test_native_resume_rejects_inconsistent_history(make_model, problem):
    model = make_model(angles=(100,))
    model.write(COILS[0])
    model.lib.fs_model_close()
    guest = model.now
    timeline = model.out / "timeline.jsonl"
    if problem == "backwards":
        guest -= 1
    elif problem == "empty":
        timeline.write_text("")
    elif problem == "forged":
        records = timeline.read_text().splitlines()
        last = json.loads(records[-1])
        last["modules"][0]["counters"]["anomalies"] = 3
        timeline.write_text("\n".join([*records[:-1], json.dumps(last)]) + "\n")
    else:
        config = json.loads(model.config.read_text())
        config["modules"][0]["addr"] = 33
        model.config.write_text(json.dumps(config))
    assert model.lib.fs_model_resume(str(model.config).encode(), guest) != 0


@pytest.mark.parametrize("polarity", ["active-high", "active-low"])
@pytest.mark.parametrize(
    "angle,present", [(0, True), (1, True), (2, False), (11, False), (12, True), (15, True)]
)
def test_sensor_polarity_changes_electrical_input_not_physical_angle(
    make_model, polarity, angle, present
):
    model = make_model(
        steps=16, magnet=12, window=6, flaps=" AB", angles=(angle,), polarity=polarity
    )
    level = present if polarity == "active-high" else not present
    assert bool(model.read() & 0x8000) is level
    assert model.state()["modules"][0]["hall"] is level
    assert model.state()["modules"][0]["angle"] == angle


def test_sensor_width_and_polarity_are_per_module_and_survive_reset(make_model):
    model = make_model(
        angles=(733, 733),
        buses=(0, 1),
        module_overrides={
            0: {"sensor_window": 3},
            1: {"sensor_window": 7, "sensor_polarity": "active-low"},
        },
    )
    # Both signals low here, for different physical reasons. After one reverse
    # step module 0 enters its narrow window; module 1 remains active-low.
    assert not model.read(bus=0, addr=32) & 0x8000
    assert not model.read(bus=1, addr=33) & 0x8000
    model.write(COILS[0])
    model.write(COILS[3])
    model.write(OFF)
    assert model.read(bus=0, addr=32) & 0x8000
    model.lib.fs_model_guest_reset()
    assert model.read(bus=0, addr=32) & 0x8000
    assert not model.read(bus=1, addr=33) & 0x8000


def test_legacy_sensor_fields_inherit_global_defaults(make_model):
    model = make_model(angles=(730,), polarity="active-low", window=3)
    config = json.loads(model.config.read_text())
    for field in ["sensor_window", "sensor_polarity"]:
        del config["modules"][0][field]
    model.config.write_text(json.dumps(config))
    assert model.lib.fs_model_init(str(model.config).encode()) == 0
    assert not model.read() & 0x8000
    del config["drum"]["sensor_polarity"]
    model.config.write_text(json.dumps(config))
    assert model.lib.fs_model_init(str(model.config).encode()) == 0
    assert model.read() & 0x8000


@pytest.mark.parametrize(
    "key,value",
    [
        ("sensor_window", 0),
        ("sensor_window", 2049),
        ("sensor_window", True),
        ("sensor_polarity", "random"),
        ("sensor_polarity", "active-low\u0000extra"),
        ("sensor_polarity", False),
    ],
)
def test_bad_normalized_sensor_fields_fail(make_model, key, value):
    model = make_model()
    config = json.loads(model.config.read_text())
    config["modules"][0][key] = value
    model.config.write_text(json.dumps(config))
    assert model.lib.fs_model_init(str(model.config).encode()) != 0
    assert model.lib.fs_model_error()


def test_live_snapshot_is_current_without_changing_evidence(make_model):
    model = make_model(angles=(730,), polarity="active-low", window=3)
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        model.write(COILS[0])
        model.write(COILS[1])
        assert model.state()["modules"][0]["angle"] == 730  # old settlement
        before = {p.name: p.read_bytes() for p in model.out.iterdir()}
        for _ in range(3):
            parent.send(b"FLAPSIM_STATE/1")
            assert model.lib.fs_control_serve(child.fileno(), model.now + 1000) == 1
            live = json.loads(parent.recv(65536))
            assert live["settled"] is False
            assert live["modules"][0]["angle"] == 731
            assert live["modules"][0]["coils"] == 9
            assert live["modules"][0]["hall"] is False
            assert live["modules"][0]["mechanics"]["sensor_window"] == 3
        assert {p.name: p.read_bytes() for p in model.out.iterdir()} == before
        model.lib.fs_model_guest_reset()
        parent.send(b"FLAPSIM_STATE/1")
        assert model.lib.fs_control_serve(child.fileno(), model.now + 1000) == 1
        reset = json.loads(parent.recv(65536))
        assert reset["guest_resets"] == 1
        assert reset["modules"] == live["modules"]
    finally:
        parent.close()
        child.close()


def test_control_protocol_rejects_bad_and_truncated_requests_without_model_failure(make_model):
    model = make_model()
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        assert model.lib.fs_control_serve(child.fileno(), 0) == 1  # no request, no block
        for request in [b"bad", b"FLAPSIM_STATE/1\0", b"FLAPSIM_STATE/1" + b"x" * 200]:
            parent.send(request)
            assert model.lib.fs_control_serve(child.fileno(), 0) == 1
            assert "error" in json.loads(parent.recv(65536))
        parent.send(b"FLAPSIM_STATE/1")
        assert model.lib.fs_control_serve(child.fileno(), 0) == 1
        assert json.loads(parent.recv(65536))["settled"] is True
        parent.close()
        assert model.lib.fs_control_serve(child.fileno(), 0) == 0
        assert not model.lib.fs_model_error()
    finally:
        parent.close()
        child.close()


def test_atomic_mechanics_change_updates_real_hall_and_preserves_physical_state(make_model):
    model = make_model(angles=(730, 730), buses=(0, 1))
    before = model.state()
    config = model.config.read_bytes()
    assert model.read() & 0x8000
    model.tick()
    unchanged = model.timeline()[-1]["modules"][1]
    result = model.control({"sensor_polarity": "active-low", "flap_offset": 700})
    assert result["mechanics_revision"] == 1
    module = result["modules"][0]
    assert module["angle"] == before["modules"][0]["angle"]
    assert (module["latch"], module["phase"]) == (0xFFE1, -1)
    assert not module["hall"] and not model.read() & 0x8000
    assert model.read(bus=1, addr=33) & 0x8000
    assert result["modules"][1]["angle"] == unchanged["angle"]
    assert result["modules"][1]["mechanics"]["sensor_polarity"] == "active-high"
    assert model.config.read_bytes() == config
    hardware = json.loads(config)
    validate_state(result, hardware)
    assert read_settlement(model.out, hardware)["mechanics_revision"] == 1
    assert model.timeline()[-1]["event"] == "mechanics_changed"
    assert model.timeline()[-1]["last_operation"]["sequence"] == model.timeline()[-1]["sequence"]
    # Historical v1 records still derive geometry from the immutable initial hardware.
    for frame in model.timeline():
        validate_state(frame, hardware, require_settled=False)


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"angle": 0},
        {"steps_per_rotation": True},
        {"flap_offset": 2048},
        {"sensor_window": 0},
        {"sensor_window": 2049},
        {"sensor_polarity": "active-low\0bad"},
        {"steps_per_rotation": 700},
        {"steps_per_rotation": 1_000_001},
        {"steps_per_rotation": 800, "magnet_position": 900},
        {"magnet_position": 0.0},
        {"flap_offset": -1},
        {"flap_offset": 100, "sensor_polarity": False},
    ],
)
def test_invalid_mechanics_are_atomic_and_leave_evidence_unchanged(make_model, patch):
    model = make_model(angles=(900,))
    before = {p.name: p.read_bytes() for p in model.out.iterdir()}
    assert model.control(patch) == {"error": "invalid_mechanics"}
    assert {p.name: p.read_bytes() for p in model.out.iterdir()} == before
    assert not model.lib.fs_model_error()


def test_mechanics_rejects_open_bus_and_running_motor_without_interrupting_reads(make_model):
    model = make_model(angles=(730,))
    assert model.event(0) == 0  # START read, captures both hall bytes
    assert model.event(6) == 0xE1
    assert model.control({"sensor_polarity": "active-low"}) == {"error": "model_busy"}
    assert model.event(6) == 0xFF  # original latched high hall input
    model.event(4)
    model.event(3)
    model.write(COILS[0])
    assert model.control({"flap_offset": 1}) == {"error": "model_busy"}
    model.write(OFF)
    assert model.control({"sensor_polarity": "active-low"})["mechanics_revision"] == 1


def test_mechanics_cas_deduplication_and_native_resume(make_model):
    model = make_model(angles=(1500,))
    patch = {"steps_per_rotation": 4096, "flap_offset": 3000, "sensor_window": 10}
    first = model.control(patch)
    before = {p.name: p.read_bytes() for p in model.out.iterdir()}
    assert model.control(patch) == first
    assert model.control({"flap_offset": 0}) == {"error": "stale_or_reused_id"}
    assert model.control({"flap_offset": 0}, identifier=2) == {"error": "revision_conflict"}
    assert {p.name: p.read_bytes() for p in model.out.iterdir()} == before
    model.lib.fs_model_guest_reset()
    reset = model.timeline()[-1]
    model.lib.fs_model_close()
    assert model.lib.fs_model_resume(str(model.config).encode(), model.now) == 0
    duplicate = model.control(patch)
    assert duplicate["mechanics_revision"] == 1 and duplicate["guest_resets"] == 1
    assert duplicate["modules"] == first["modules"]
    assert model.timeline()[-1] == reset
    second = model.control({"flap_offset": 0}, identifier=5, revision=1)
    assert second["mechanics_revision"] == 2
    assert model.control(patch) == {"error": "stale_or_reused_id"}
    assert model.control({"flap_offset": 1}, identifier=4, revision=2) == {
        "error": "stale_or_reused_id"
    }
    model.lib.fs_model_close()
    assert model.lib.fs_model_resume(str(model.config).encode(), model.now) == 0
    assert model.control({"flap_offset": 0}, identifier=5, revision=1)["guest_resets"] == 2


def test_rotation_change_rejects_inherited_geometry_and_never_wraps_angle(make_model):
    model = make_model(angles=(1000,))
    assert model.control({"steps_per_rotation": 800}) == {"error": "invalid_mechanics"}
    changed = model.control({"steps_per_rotation": 1001})
    assert changed["modules"][0]["angle"] == 1000
    assert changed["modules"][0]["mechanics"]["steps_per_rotation"] == 1001


@pytest.mark.parametrize(
    "problem", ["initial", "flaps", "geometry", "angle", "receipt", "version", "missing", "entry"]
)
def test_native_resume_rejects_invalid_mechanics_overlay(make_model, problem):
    model = make_model(angles=(730,))
    model.control({"sensor_polarity": "active-low"})
    model.lib.fs_model_close()
    path = model.out / "timeline.jsonl"
    lines = path.read_text().splitlines()
    last = json.loads(lines[-1])
    if problem == "initial":
        last["initial_mechanics"][0]["sensor_window"] += 1
    elif problem == "entry":
        last["modules"][0] = 1
    elif problem == "flaps":
        last["initial_flaps"] = last["initial_flaps"][::-1]
    elif problem == "geometry":
        last["modules"][0]["mechanics"]["magnet_position"] = 2048
    elif problem == "angle":
        last["modules"][0]["angle"] = 2048
    elif problem == "receipt":
        last["last_operation"]["command"]["patch"]["sensor_polarity"] = "active-high"
    elif problem == "version":
        last["mechanics_version"] = 2
    else:
        del last["modules"][0]["mechanics"]
    path.write_text("\n".join([*lines[:-1], json.dumps(last)]) + "\n")
    assert model.lib.fs_model_resume(str(model.config).encode(), model.now) != 0


@pytest.fixture
def model_http(make_model):
    import threading

    from flapsim.control import ControlServer

    model = make_model(angles=(730,))
    hardware = json.loads(model.config.read_text())
    server = ControlServer(0, model.out, hardware)
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    server.attach(parent)
    stopped = threading.Event()

    def serve():
        while not stopped.is_set():
            if not model.lib.fs_control_serve(child.fileno(), model.now):
                break
            stopped.wait(0.001)

    worker = threading.Thread(target=serve)
    worker.start()
    try:
        yield model, server
    finally:
        server.close()
        stopped.set()
        worker.join(1)
        assert not worker.is_alive()
        child.close()


def http_control(server, *, data=None, path="/api/emu/modules/0/32/mech", headers=None):
    import urllib.error
    import urllib.request

    headers = {
        "Authorization": "Bearer " + server.token,
        "Content-Type": "application/json",
        **(headers or {}),
    }
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(
            urllib.request.Request(server.endpoint + path, headers=headers, data=data), timeout=2
        )
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read())


def test_http_mechanics_operations_audit_and_replay_use_the_real_model(model_http):
    from flapsim.report import replay_data, write_replay

    model, server = model_http
    command = {
        "id": 1,
        "expected_revision": 0,
        "patch": {"sensor_polarity": "active-low", "flap_offset": 700},
    }
    assert http_control(server, path="/api/emu/operations/1")[0] == 404
    code, applied = http_control(server, data=json.dumps(command).encode())
    assert code == 202 and applied["status"] == "applied"
    operation = applied["operation"]
    assert operation["revision"] == 1
    assert http_control(server, path="/api/emu/operations/1")[1]["operation"] == operation
    assert http_control(server, data=json.dumps(command).encode())[1]["operation"] == operation
    assert len([v for v in model.timeline() if v["event"] == "mechanics_changed"]) == 1
    command["patch"] = {"flap_offset": 0}
    assert http_control(server, data=json.dumps(command).encode()) == (
        409,
        {"error": "stale_or_reused_id"},
    )
    command["id"] = 2
    assert http_control(server, data=json.dumps(command).encode()) == (
        409,
        {"error": "revision_conflict"},
    )
    command["expected_revision"] = 1
    assert http_control(server, data=json.dumps(command).encode())[0] == 202
    assert http_control(server, path="/api/emu/operations/1")[1]["status"] == "unknown"
    assert http_control(server, path="/api/emu/state")[1]["state"]["mechanics_revision"] == 2
    journal = [json.loads(line) for line in (model.out / "control.jsonl").read_text().splitlines()]
    assert [v["phase"] for v in journal] == ["intent", "result"] * 5
    assert journal[1]["result"]["last_operation"] == operation
    assert server.token not in (model.out / "control.jsonl").read_text()
    assert all(v["run_id"] == server.run_id for v in journal)
    frames, warning = replay_data(model.out)
    assert warning is None and frames[-1]["mechanics_revision"] == 2
    assert frames[-2]["mechanics_revision"] == 1
    write_replay(model.out, frames, warning)
    assert '"mechanics_revision": 1' in (model.out / "replay.html").read_text()


@pytest.mark.parametrize(
    "data,headers,path,code",
    [
        (b"{", {}, "/api/emu/modules/0/32/mech", 400),
        (b'{"id":1,"id":2}', {}, "/api/emu/modules/0/32/mech", 400),
        (b'{"patch":{"sensor_window":NaN}}', {}, "/api/emu/modules/0/32/mech", 400),
        (b"{}" * 1100, {}, "/api/emu/modules/0/32/mech", 413),
        (b"{}", {"Content-Type": "text/plain"}, "/api/emu/modules/0/32/mech", 415),
        (b"{}", {"Origin": "http://foreign.example"}, "/api/emu/modules/0/32/mech", 403),
        (b"{}", {"Host": "foreign.example"}, "/api/emu/modules/0/32/mech", 403),
        (b"{}", {"Authorization": "Bearer invalid"}, "/api/emu/modules/0/32/mech", 401),
        (b"{}", {}, "/api/emu/modules/1/32/mech", 404),
        (b"{}", {}, "/api/emu/modules/0/32/mech", 422),
        (
            b'{"id":true,"expected_revision":0,"patch":{"flap_offset":1}}',
            {},
            "/api/emu/modules/0/32/mech",
            422,
        ),
        (
            b'{"id":1,"expected_revision":0,"patch":{"flap_offset":1.5}}',
            {},
            "/api/emu/modules/0/32/mech",
            422,
        ),
        (
            b'{"id":1,"expected_revision":0,"patch":{"flap_offset":true}}',
            {},
            "/api/emu/modules/0/32/mech",
            422,
        ),
        (
            b'{"id":1,"expected_revision":0,"patch":{"sensor_window":0}}',
            {},
            "/api/emu/modules/0/32/mech",
            422,
        ),
        (
            b'{"id":1,"expected_revision":0,"patch":{"fault":"dead"}}',
            {},
            "/api/emu/modules/0/32/mech",
            422,
        ),
        (
            b'{"id":1,"expected_revision":0,"patch":{"flap_offset":1},"extra":0}',
            {},
            "/api/emu/modules/0/32/mech",
            422,
        ),
    ],
)
def test_http_rejection_never_mutates_model(model_http, data, headers, path, code):
    model, server = model_http
    before = model.timeline()
    assert http_control(server, data=data, headers=headers, path=path)[0] == code
    assert model.timeline() == before
    assert http_control(server, path="/api/emu/state")[0] == 200


def test_lost_acknowledgement_never_reapplies_a_mechanics_command(make_model):
    model = make_model()
    command = {"id": 1, "expected_revision": 0, "bus": 0, "addr": 32, "patch": {"flap_offset": 5}}
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    parent.sendall(b"FLAPSIM_MECH/1 " + json.dumps(command).encode())
    parent.close()  # Simulate the client disappearing after sending its command.
    with child:
        assert model.lib.fs_control_serve(child.fileno(), model.now) == 0
    receipt = model.state()["last_operation"]
    assert receipt["command"] == command
    before = model.timeline()
    assert model.control({"flap_offset": 5})["mechanics_revision"] == 1
    assert model.timeline() == before
    second = model.control({"flap_offset": 9}, identifier=2, revision=1)
    assert second["last_operation"]["command"]["id"] == 2
    # Replacing the live receipt never removes the earlier append-only timeline record.
    applied = [
        frame["last_operation"]
        for frame in model.timeline()
        if frame["event"] == "mechanics_changed"
    ]
    assert applied == [receipt, second["last_operation"]]


def test_native_resume_restores_overlay_before_validating_angle(make_model):
    model = make_model(angles=(2047,))
    model.control({"steps_per_rotation": 4096})
    model.write(COILS[0])
    model.write(COILS[1])
    model.write(OFF)
    assert model.state()["modules"][0]["angle"] == 2048
    model.lib.fs_model_guest_reset()
    model.lib.fs_model_close()
    assert model.lib.fs_model_resume(str(model.config).encode(), model.now) == 0
    resumed = model.control({"steps_per_rotation": 4096})
    assert resumed["modules"][0]["angle"] == 2048
    validate_state(resumed, json.loads(model.config.read_text()))


def test_http_lost_audit_commit_reports_unknown_even_after_model_applied(model_http, monkeypatch):
    import flapsim.control

    model, server = model_http
    original = os.fsync
    count = 0

    def fail_result(descriptor):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("injected result fsync failure")
        original(descriptor)

    monkeypatch.setattr(flapsim.control.os, "fsync", fail_result)
    code, response = http_control(
        server, data=b'{"id":1,"expected_revision":0,"patch":{"flap_offset":5}}'
    )
    assert code == 503 and response == {"error": "result_unknown", "id": 1}
    assert model.state()["mechanics_revision"] == 1
    journal = [json.loads(line) for line in (model.out / "control.jsonl").read_text().splitlines()]
    assert [entry["phase"] for entry in journal] == ["intent", "result", "unknown"]
    assert journal[-1]["stage"] == "result_audit" and journal[-1]["channel"] == "closed"
    assert journal[-1]["error"] == "OSError"
    assert journal[-1]["command"]["id"] == 1
    assert http_control(server, path="/api/emu/state")[0] == 503


def test_http_slow_body_is_bounded_and_does_not_touch_model(model_http):
    import http.client
    import time

    model, server = model_http
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=2)
    try:
        connection.putrequest("POST", "/api/emu/modules/0/32/mech")
        connection.putheader("Authorization", "Bearer " + server.token)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", "100")
        connection.endheaders()
        started = time.monotonic()
        response = connection.getresponse()
        assert response.status == 408 and time.monotonic() - started < 1.8
        response.read()
        assert len(model.timeline()) == 1
    finally:
        connection.close()


def test_private_transport_rejects_oversize_even_with_a_valid_maximum_size_prefix(make_model):
    model = make_model()
    command = {"id": 1, "expected_revision": 0, "bus": 0, "addr": 32, "patch": {"flap_offset": 5}}
    prefix = b"FLAPSIM_MECH/1 "
    encoded = json.dumps(command).encode()
    valid = prefix + encoded[:-1] + b" " * (2048 - len(prefix) - len(encoded)) + b"}"
    assert len(valid) == 2048
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with parent, child:
        before = model.timeline()
        parent.sendall(valid + b"junk")
        assert model.lib.fs_control_serve(child.fileno(), model.now) == 1
        assert "error" in json.loads(parent.recv(65536))
        assert model.timeline() == before
        # Prove the truncated prefix would have been a valid command, rather than
        # relying on invalid JSON to reject it. Linux recv(MSG_TRUNC) returns the
        # original sequenced-packet length and the C size guard rejects that packet.
        parent.sendall(valid)
        assert model.lib.fs_control_serve(child.fileno(), model.now) == 1
        assert json.loads(parent.recv(65536))["mechanics_revision"] == 1


def test_native_reset_generations_persist_before_any_mechanics_edit(make_model):
    model = make_model()
    for generation in range(1, 4):
        assert resume(model, model.now + 1000000) == 0
        assert model.activity()["guest_resets"] == generation
    reply = model.control({"sensor_window": 20})
    assert reply["guest_resets"] == 3
    assert resume(model, model.now + 1000000) == 0
    assert model.control({"sensor_window": 20})["guest_resets"] == 4


@pytest.mark.parametrize("generation", [-1, True, "1", None])
def test_native_resume_rejects_invalid_reset_generation(make_model, generation):
    model = make_model()
    model.lib.fs_model_close()
    timeline = model.out / "timeline.jsonl"
    items = [json.loads(line) for line in timeline.read_text().splitlines()]
    items[-1]["guest_resets"] = generation
    timeline.write_text("".join(json.dumps(item) + "\n" for item in items))
    assert model.lib.fs_model_resume(str(model.config).encode(), model.now) != 0


def test_native_resume_accepts_legacy_records_without_reset_generation(make_model):
    model = make_model()
    model.lib.fs_model_guest_reset()
    model.lib.fs_model_close()
    timeline = model.out / "timeline.jsonl"
    items = [json.loads(line) for line in timeline.read_text().splitlines()]
    for item in items:
        item.pop("guest_resets")
    timeline.write_text("".join(json.dumps(item) + "\n" for item in items))
    assert model.lib.fs_model_resume(str(model.config).encode(), model.now) == 0
    assert model.control({"sensor_window": 20})["guest_resets"] == 1


def faulted(make_model, faults, **kwargs):
    return make_model(module_overrides={0: {"faults": faults}}, **kwargs)


def fault_record(model, index=0):
    """The live fault record, from the activity file the next tick rewrites."""
    model.tick()
    return model.activity()["faults"][index]


def turn(model, steps, addr=32):
    for step in range(steps):
        model.write(COILS[(step + 1) % 4], addr=addr)
    model.write(OFF, addr=addr)


def read_cycle(model):
    acked = model.event(0) == 0
    low, high = model.event(6), model.event(6)
    model.event(4)
    model.event(3)
    return acked, low | high << 8


@pytest.mark.parametrize(
    "fault, at_magnet, away",
    [
        ("stuck_active", True, True),
        ("stuck_inactive", False, False),
        ("inverted", False, True),
    ],
)
def test_sensor_faults_change_what_firmware_reads_not_the_physical_sensor(
    make_model, fault, at_magnet, away
):
    for angle, expected in [(730, at_magnet), (0, away)]:
        model = faulted(make_model, {"sensor": fault}, angles=(angle,))
        assert bool(model.read() & 0x8000) is expected
        assert model.state()["modules"][0]["hall"] is (angle == 730)
        assert fault_record(model) == {
            "sensor": fault,
            "events": int(expected != (angle == 730)),
            "bus": 0,
            "addr": 32,
        }
        model.lib.fs_model_close()


def test_only_reads_sample_a_faulted_sensor(make_model):
    def levels(writes):
        model = faulted(make_model, {"sensor": "noisy", "sensor_noise": 0.3}, angles=(0,))
        values = []
        for _ in range(100):
            for _ in range(writes):
                model.write(OFF)
            values.append(bool(model.read() & 0x8000))
        events = fault_record(model)["events"]
        model.lib.fs_model_close()
        return values, events

    # Coil writes between reads neither shift the noise stream nor count as fault events.
    assert levels(3) == levels(0)
    model = faulted(make_model, {"sensor": "stuck_active"}, angles=(0,))
    for _ in range(5):
        model.write(OFF)
    assert fault_record(model)["events"] == 0
    model.read()
    assert fault_record(model)["events"] == 1


def test_noisy_sensor_flips_some_reads_reproducibly(make_model):
    def levels():
        model = faulted(make_model, {"sensor": "noisy", "sensor_noise": 0.3}, angles=(0,))
        values = [bool(model.read() & 0x8000) for _ in range(200)]
        events = fault_record(model)["events"]
        model.lib.fs_model_close()
        return values, events

    first, events = levels()
    assert 30 < events < 90 and events == sum(first)  # Physically inactive: flips read high.
    assert levels() == (first, events)


def test_dead_motor_energizes_without_turning(make_model):
    model = faulted(make_model, {"motor": "dead"}, angles=(5,))
    turn(model, 8)
    module = model.state()["modules"][0]
    assert module["angle"] == 5
    assert module["counters"]["forward"] == 0 and module["counters"]["stalls"] == 0
    assert module["counters"]["writes"] == 9
    assert fault_record(model)["events"] == 7  # The first energize only sets the phase.


def test_slipping_motor_misses_some_steps(make_model):
    model = faulted(make_model, {"motor": "slip", "slip": 0.25}, angles=(0,))
    turn(model, 401)
    module = model.state()["modules"][0]
    missed = fault_record(model)["events"]
    assert 60 < missed < 140
    assert module["angle"] == module["counters"]["forward"] == 400 - missed


def test_missing_module_answers_like_an_absent_address(make_model):
    model = faulted(make_model, {"i2c": "missing"})
    assert model.event(1) != 0
    assert model.event(0xE705) != 0
    model.event(3)
    assert read_cycle(model) == (False, 0xFFFF)
    record = fault_record(model)
    state = model.activity()
    assert state["counters"]["anomalies"] == 0 and state["counters"]["writes"] == 0
    assert record == {"i2c": "missing", "events": 2, "bus": 0, "addr": 32}


def test_module_disappears_at_the_configured_guest_time(make_model):
    model = faulted(make_model, {"i2c": "disappear", "disappear_at": 0.5})
    model.write(COILS[0])
    model.now = 500_000_000
    assert model.event(1) != 0
    model.event(3)
    assert model.activity()["counters"]["writes"] == 1
    assert fault_record(model)["events"] == 1


def test_nack_bursts_and_read_corruption_are_reproducible(make_model):
    def run(faults):
        model = faulted(make_model, faults, angles=(0,))
        results = [read_cycle(model) for _ in range(100)]
        events = fault_record(model)["events"]
        assert model.activity()["counters"]["anomalies"] == 0
        model.lib.fs_model_close()
        return results, events

    nacks, events = run({"i2c": "nack", "nack": 0.2})
    assert sum(not acked for acked, _ in nacks) == events and 5 < events < 40
    assert all(value == 0xFFFF for acked, value in nacks if not acked)
    assert run({"i2c": "nack", "nack": 0.2})[0] == nacks
    reads, events = run({"i2c": "corrupt", "corrupt": 0.5})
    changed = sum(value != 0x7FE1 for _, value in reads)
    assert changed > 20 and events >= changed
    assert all(bin(value ^ 0x7FE1).count("1") <= 2 for _, value in reads)


def test_healthy_runs_report_no_faults_and_faulted_runs_list_only_faulted_modules(make_model):
    model = make_model(angles=(0, 0), buses=(0, 0))
    model.tick()
    assert "faults" not in model.activity() and "faults" not in model.state()
    model.lib.fs_model_close()
    model = make_model(
        angles=(0, 0), buses=(0, 0), module_overrides={1: {"faults": {"motor": "dead"}}}
    )
    model.tick()
    assert model.activity()["faults"] == [{"motor": "dead", "events": 0, "bus": 0, "addr": 33}]
    assert model.state()["faults"] == model.activity()["faults"]


@pytest.mark.parametrize(
    "faults",
    [
        {},
        {"sensor": "melted"},
        {"sensor": "noisy"},
        {"sensor": "stuck_active", "sensor_noise": 0.1},
        {"motor": "slip", "slip": 0},
        {"motor": "slip", "slip": 1.5},
        {"i2c": "disappear"},
        {"i2c": "disappear", "disappear_at": -1},
        {"i2c": "nack", "nack": True},
        {"power": "cycle"},
    ],
)
def test_bad_fault_configuration_is_rejected(faults):
    document = {
        "chip": "esp32",
        "buses": [{"id": 0, "modules": [{"addr": 32, "faults": faults}]}],
        "drum": {
            "steps_per_rotation": 2048,
            "magnet_position": 730,
            "sensor_window": 40,
            "flaps": " A",
        },
        "start_angles": [0],
    }
    with pytest.raises(ValueError):
        normalize_hardware(document)


def test_model_rejects_unknown_fault_parameters_beside_a_valid_fault(make_model):
    model = faulted(make_model, {"sensor": "stuck_active"})
    config = json.loads(model.config.read_text())
    module = config["modules"][0]
    module["faults"]["power"] = "cycle"
    model.lib.fs_model_close()
    model.config.write_text(json.dumps(config))
    assert model.lib.fs_model_init(str(model.config).encode()) != 0
    assert b"faults.power" in model.lib.fs_model_error()


def test_native_power_checkpoint_preserves_moving_state_and_resumes_once(make_model):
    model = make_model(angles=(100, 200), buses=(0, 0))
    model.control({"sensor_window": 41})
    for coils in COILS[:3]:
        model.write(coils)
    before_generation = model.activity()["guest_resets"]
    model.lib.fs_model_power_checkpoint.argtypes = [ctypes.c_int64]
    model.lib.fs_model_power_checkpoint.restype = ctypes.c_int
    assert model.lib.fs_model_power_checkpoint(model.now) == 0
    checkpoint = model.timeline()[-1]
    assert checkpoint["event"] == "controller_power_off"
    assert checkpoint["guest_resets"] == before_generation
    assert checkpoint["modules"][0]["angle"] == 102
    assert checkpoint["modules"][0]["energized"]
    model.lib.fs_model_close()
    assert model.lib.fs_model_resume(str(model.config).encode(), model.now) == 0
    restored = model.timeline()[-1]
    assert restored["event"] == "guest_reset"
    assert restored["guest_resets"] == before_generation + 1
    assert restored["modules"] == checkpoint["modules"]
    assert restored["last_operation"] == checkpoint["last_operation"]
    model.write(COILS[3])
    model.write(OFF)
    assert model.state()["counters"]["stalls"] == 0
