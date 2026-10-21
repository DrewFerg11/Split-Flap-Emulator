"""Drive the real C model directly, without a QEMU build or firmware logic."""

import ctypes
import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from flapsim.hardware import normalize_hardware, write_model_config
from flapsim.state import read_settlement

ROOT = Path(__file__).resolve().parents[1]
CORE = ["bus", "config", "drum", "io", "pcf8575", "resume", "stepper"]
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
    ):
        self.lib, self.out, self.now = library, out, 0
        modules = [
            {"id": b, "modules": [32 + i for i, bus in enumerate(buses) if bus == b]}
            for b in sorted(set(buses))
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
    assert model.lib.fs_model_error()
