"""Physical configuration validation, ordering and reproducible normalization."""

import copy
import json
from pathlib import Path

import pytest
import yaml

from flapsim.hardware import load_hardware, normalize_hardware, positions, write_model_config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def document():
    return yaml.safe_load((ROOT / "hardware/esp32-8-single.yaml").read_text())


def test_both_layouts_load_and_have_stable_order():
    for name, count in [("esp32-8-single", 8), ("esp32-16-dual", 16)]:
        hardware = load_hardware(name)
        assert len(hardware["modules"]) == count
        identities = [(m["bus"], m["addr"]) for m in hardware["modules"]]
        assert identities == sorted(set(identities))
        assert hardware == load_hardware(name)
        assert hardware["schema_version"] == 1


def test_seed_vectors_and_override(document):
    # xorshift32 reference vector beginning with 270369, 67634689, 2647435461.
    assert positions(1, 3, 1_000_000) == [270369, 634689, 435461]
    first = normalize_hardware(document, seed=7)
    assert first == normalize_hardware(document, seed=7)
    assert first != normalize_hardware(document, seed=8)
    assert normalize_hardware(document, seed=0) == normalize_hardware(document, seed=0)
    document["start_angles"] = "random"
    random = normalize_hardware(document)
    document["start_angles"] = {"seed": random["seed"]}
    assert random == normalize_hardware(document)


def test_explicit_angles_use_sorted_identity_order(document):
    document["buses"][0]["modules"].reverse()
    document["buses"].reverse()
    document["start_angles"] = list(range(8))
    result = normalize_hardware(document)
    assert result["seed"] is None
    assert [(m["addr"], m["angle"]) for m in result["modules"]] == list(
        zip(range(32, 40), range(8), strict=True)
    )


@pytest.mark.parametrize(
    "change, message",
    [
        ({"chip": "esp32s3"}, "chip must be esp32"),
        ({"extra": True}, "unknown fields"),
        ({"buses": []}, "nonempty"),
        ({"buses": [{"id": 2, "modules": [32]}]}, "bus.id"),
        ({"buses": [{"id": True, "modules": [32]}]}, "bus.id"),
        ({"buses": [{"id": 0, "modules": [32, 32]}]}, "duplicate module"),
        ({"buses": [{"id": 0, "modules": [0x48]}]}, "temperature sensor"),
        ({"buses": [{"id": 0, "modules": [0x78]}]}, "module address"),
        ({"buses": [{"id": 0, "modules": [0x10]}]}, "PCF8575 address"),
        ({"buses": [{"id": 0, "modules": ["0x20"]}]}, "integer"),
        ({"buses": [{"id": 0, "modules": []}]}, "at least one"),
        ({"buses": [{"id": 0, "modules": [32]}, {"id": 0, "modules": []}]}, "duplicate bus"),
        ({"start_angles": [0]}, "one angle per module"),
        ({"start_angles": [2048] * 8}, "start angle"),
        ({"start_angles": {"seed": -1}}, "seed"),
        ({"start_angles": {"seed": True}}, "seed"),
        ({"start_angles": "unknown"}, "start_angles"),
    ],
)
def test_invalid_configuration(document, change, message):
    document.update(copy.deepcopy(change))
    with pytest.raises(ValueError, match=message):
        normalize_hardware(document)


@pytest.mark.parametrize(
    "key,value",
    [
        ("steps_per_rotation", 0),
        ("steps_per_rotation", True),
        ("flaps", "AA"),
        ("flaps", "A\n"),
        ("flaps", "Aé"),
        ("magnet_position", 2048),
        ("magnet_position", -1),
        ("sensor_window", 0),
        ("sensor_window", 2049),
    ],
)
def test_invalid_mechanics(document, key, value):
    document["drum"][key] = value
    with pytest.raises(ValueError):
        normalize_hardware(document)


def test_normalized_model_output_paths(tmp_path):
    path = write_model_config(load_hardware("esp32-16-dual"), tmp_path, trace=True)
    config = json.loads(path.read_text())
    assert len(config["modules"]) == 16
    assert config["outputs"] == {
        name: str(tmp_path / filename)
        for name, filename in {
            "state": "state.json",
            "activity": "activity.json",
            "timeline": "timeline.jsonl",
            "trace": "i2c.log",
        }.items()
    }


def test_duplicate_yaml_fields_are_rejected(tmp_path):
    path = tmp_path / "duplicate.yaml"
    path.write_text("chip: esp32\nchip: esp32s3\n")
    with pytest.raises(ValueError, match="duplicate field 'chip'"):
        load_hardware(path)


def test_seed_override_does_not_hide_invalid_config(document):
    document["start_angles"] = "invalid"
    with pytest.raises(ValueError, match="start_angles"):
        normalize_hardware(document, seed=7)


def test_realistic_layouts_have_reproducible_independent_mechanics():
    for name, count in [("esp32-8-single-realistic", 8), ("esp32-16-dual-realistic", 16)]:
        hardware = load_hardware(name)
        assert hardware == load_hardware(name)
        assert hardware != load_hardware(name, seed=19)
        assert len(hardware["modules"]) == count
        for key, lower, upper in [
            ("steps_per_rotation", 2036, 2040),
            ("magnet_position", 728, 732),
            ("flap_offset", 0, 4),
        ]:
            values = [m[key] for m in hardware["modules"]]
            assert all(lower <= v <= upper for v in values)
            assert len(set(values)) > 1
        assert all(0 <= m["angle"] < m["steps_per_rotation"] for m in hardware["modules"])


def test_mechanics_streams_do_not_change_starts_or_each_other(document):
    ideal = normalize_hardware(document)
    document["drum"]["magnet_position"] = {"mean": 730, "spread": 2}
    varied = normalize_hardware(document)
    for a, b in zip(ideal["modules"], varied["modules"], strict=True):
        assert a["angle"] == b["angle"]
        assert a["steps_per_rotation"] == b["steps_per_rotation"]
        assert a["flap_offset"] == b["flap_offset"]
    document["start_angles"] = "random"
    random = normalize_hardware(document)
    document["start_angles"] = {"seed": random["seed"]}
    assert normalize_hardware(document) == random


@pytest.mark.parametrize(
    "key,value",
    [
        ("steps_per_rotation", {"mean": 2038, "spread": -1}),
        ("steps_per_rotation", {"mean": 2038, "spread": True}),
        ("steps_per_rotation", {"mean": 4, "spread": 1}),
        ("steps_per_rotation", {"mean": 1_000_000, "spread": 1}),
        ("steps_per_rotation", {"mean": 2038}),
        ("magnet_position", {"mean": 0, "spread": 1}),
        ("magnet_position", {"mean": 2047, "spread": 1}),
        ("flap_offset", -1),
        ("flap_offset", {"mean": 2048, "spread": 0}),
    ],
)
def test_invalid_variance_rejected(document, key, value):
    document["drum"][key] = value
    with pytest.raises(ValueError):
        normalize_hardware(document)


def test_variance_checks_smallest_possible_rotation(document):
    document["drum"]["steps_per_rotation"] = {"mean": 100, "spread": 10}
    document["drum"]["magnet_position"] = 90
    with pytest.raises(ValueError, match="magnet_position"):
        normalize_hardware(document)


def test_perfect_presets_preserve_original_mechanics():
    for name in ["esp32-8-single", "esp32-16-dual"]:
        assert load_hardware(name + "-perfect") == load_hardware(name)
        assert all(
            m["sensor_window"] == 40 and m["sensor_polarity"] == "active-high"
            for m in load_hardware(name + "-perfect")["modules"]
        )


def test_seeded_sensor_variation_and_explicit_module_overrides(document):
    document["drum"].update(sensor_window={"mean": 40, "spread": 8}, sensor_polarity="random")
    random = normalize_hardware(document)
    assert random == normalize_hardware(document)
    assert {m["sensor_polarity"] for m in random["modules"]} == {"active-high", "active-low"}
    assert len({m["sensor_window"] for m in random["modules"]}) > 1
    assert all(32 <= m["sensor_window"] <= 48 for m in random["modules"])
    document["buses"][0]["modules"][2] = {
        "addr": 34,
        "steps_per_rotation": 4096,
        "magnet_position": 3000,
        "flap_offset": 3200,
        "sensor_window": 73,
        "sensor_polarity": "active-low",
    }
    changed = normalize_hardware(document)
    for i in range(8):
        if i != 2:
            assert changed["modules"][i] == random["modules"][i]
    assert changed["modules"][2] == {
        **document["buses"][0]["modules"][2],
        "bus": 0,
        "angle": positions(7, 8, 4096)[2],
    }
    document["buses"][0]["modules"].reverse()
    assert normalize_hardware(document) == changed
    assert changed != normalize_hardware(document, seed=19)


@pytest.mark.parametrize(
    "entry",
    [
        {"magnet_position": 730},
        {"addr": 32, "offsets": 0},
        {"addr": True},
        {"addr": 32, "sensor_window": False},
        {"addr": 32, "sensor_window": {"mean": 40, "spread": 41}},
        {"addr": 32, "sensor_polarity": False},
        {"addr": 32, "sensor_polarity": "HIGH"},
        {"addr": 32, "sensor_polarity": ["active-high"]},
        {"addr": 32, "steps_per_rotation": 64},
    ],
)
def test_invalid_per_module_mechanics(document, entry):
    document["buses"][0]["modules"][0] = entry
    with pytest.raises(ValueError):
        normalize_hardware(document)


def test_mixed_module_entries_reject_duplicates(document):
    document["buses"][0]["modules"][1] = {"addr": 32}
    with pytest.raises(ValueError, match="duplicate module"):
        normalize_hardware(document)


def test_explicit_start_angles_use_each_module_limits(document):
    document["buses"][0]["modules"][0] = {"addr": 32, "steps_per_rotation": 4096}
    document["start_angles"] = [4095] + [0] * 7
    assert normalize_hardware(document)["modules"][0]["angle"] == 4095
    document["buses"][0]["modules"][0]["steps_per_rotation"] = 2048
    with pytest.raises(ValueError, match="start angle"):
        normalize_hardware(document, seed=7)


def test_calibration_fixture_offsets_match_true_electrical_edges():
    hardware = load_hardware("esp32-8-single-calibration")
    scenario = yaml.safe_load((ROOT / "scenarios/calibration-8.yaml").read_text())
    settings = next(
        s["http"]["body"]
        for s in scenario["steps"]
        if "http" in s and s["http"].get("method") == "POST"
    )
    offsets = [int(v) for v in settings["wireOffsets"].split(",")]
    assert len(set(offsets)) == len(hardware["modules"]) == 8
    assert min(offsets) < 0 < max(offsets)
    for module, offset in zip(hardware["modules"], offsets, strict=True):
        edge = module["magnet_position"]
        if module["sensor_polarity"] == "active-low":
            edge += module["sensor_window"]
        assert (edge - module["flap_offset"]) % module["steps_per_rotation"] == 730 + offset


def test_faults_normalize_with_reproducible_per_module_streams(document):
    document["buses"][0]["modules"][3] = {"addr": 0x23, "faults": {"sensor": "stuck_inactive"}}
    document["buses"][0]["modules"][5] = {
        "addr": 0x25,
        "faults": {"motor": "slip", "slip": 0.25, "i2c": "disappear", "disappear_at": 1.5},
    }
    hardware = normalize_hardware(document, seed=7)
    faulted = {m["addr"]: m["faults"] for m in hardware["modules"] if "faults" in m}
    assert faulted.keys() == {0x23, 0x25}
    assert {k: v for k, v in faulted[0x25].items() if k != "seed"} == {
        "motor": "slip",
        "slip": 0.25,
        "i2c": "disappear",
        "disappear_at_ns": 1_500_000_000,
    }
    assert faulted[0x23]["seed"] != faulted[0x25]["seed"]
    assert all(0 < f["seed"] <= 0xFFFFFFFF for f in faulted.values())
    assert hardware == normalize_hardware(document, seed=7)
    other = normalize_hardware(document, seed=8)
    assert [m.get("faults", {}).get("seed") for m in other["modules"]] != [
        m.get("faults", {}).get("seed") for m in hardware["modules"]
    ]
    # Faults never change geometry or starts.
    plain = normalize_hardware(
        yaml.safe_load((ROOT / "hardware/esp32-8-single.yaml").read_text()), seed=7
    )
    strip = [{k: v for k, v in m.items() if k != "faults"} for m in hardware["modules"]]
    assert strip == plain["modules"]


@pytest.mark.parametrize(
    "faults, message",
    [
        ({}, "must name a sensor, motor or i2c fault"),
        ({"sensor": "melted"}, "faults.sensor must be one of"),
        ({"motor": "slip"}, "need exactly the parameters ['slip']"),
        ({"motor": "dead", "slip": 0.5}, "need exactly the parameters []"),
        ({"sensor": "noisy", "sensor_noise": 0}, "probability in (0, 1]"),
        ({"i2c": "nack", "nack": 1.5}, "probability in (0, 1]"),
        ({"i2c": "corrupt", "corrupt": True}, "probability in (0, 1]"),
        ({"i2c": "disappear", "disappear_at": 0}, "guest seconds > 0"),
        ({"i2c": "disappear", "disappear_at": 1e-10}, "guest seconds > 0"),
        ({"i2c": "disappear", "disappear_at": float("inf")}, "guest seconds > 0"),
        ({"i2c": "disappear", "disappear_at": 1e10}, "guest seconds > 0"),
        ({"sensor": "inverted", "colour": "red"}, "colour"),
    ],
)
def test_invalid_faults(document, faults, message):
    document["buses"][0]["modules"][0] = {"addr": 0x20, "faults": faults}
    with pytest.raises(ValueError) as error:
        normalize_hardware(document, seed=7)
    assert message in str(error.value)


def test_fault_fixtures_load():
    stuck = load_hardware("esp32-8-single-stuck-sensor")
    lost = load_hardware("esp32-8-single-module-lost")
    assert [m["faults"]["sensor"] for m in stuck["modules"] if "faults" in m] == ["stuck_inactive"]
    assert [m["faults"]["disappear_at_ns"] for m in lost["modules"] if "faults" in m] == [
        90_000_000_000
    ]
