"""Strict physical-hardware configuration and a versioned model JSON boundary."""

import json
import secrets
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class HardwareLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = super().construct_mapping(node, deep=deep)
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    "hardware mapping",
                    node.start_mark,
                    f"duplicate field {key!r}",
                    key_node.start_mark,
                )
            seen.add(key)
        return result


def mapping(value, name, keys):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    unknown = value.keys() - keys
    missing = keys - value.keys()
    if unknown or missing:
        raise ValueError(
            f"{name}: unknown fields {sorted(map(str, unknown))}; missing {sorted(missing)}"
        )
    return value


def integer(value, name, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{name} must be an integer in [{lower}, {upper}]")
    return value


def positions(seed, count, steps):
    """Specified xorshift32 sequence; reproducible across Python/C library versions."""
    value = seed or 0x6D2B79F5
    result = []
    for _ in range(count):
        value ^= (value << 13) & 0xFFFFFFFF
        value ^= value >> 17
        value ^= (value << 5) & 0xFFFFFFFF
        value &= 0xFFFFFFFF
        result.append(value % steps)
    return result


def normalize_hardware(document, *, seed=None):
    document = mapping(document, "hardware", {"chip", "buses", "drum", "start_angles"})
    if document["chip"] != "esp32":
        raise ValueError("chip must be esp32 (ESP32-C3/S3 are outside the MVP)")
    drum = mapping(
        document["drum"],
        "drum",
        {
            "steps_per_rotation",
            "flaps",
            "magnet_position",
            "sensor_window",
        },
    )
    steps = integer(drum["steps_per_rotation"], "drum.steps_per_rotation", 4, 1_000_000)
    flaps = drum["flaps"]
    if (
        not isinstance(flaps, str)
        or not 2 <= len(flaps) <= min(95, steps)
        or len(set(flaps)) != len(flaps)
        or any(not 32 <= ord(c) <= 126 for c in flaps)
    ):
        raise ValueError(
            "drum.flaps must contain 2–95 unique printable ASCII characters, at most one per step"
        )
    integer(drum["magnet_position"], "drum.magnet_position", 0, steps - 1)
    integer(drum["sensor_window"], "drum.sensor_window", 1, steps)
    buses = document["buses"]
    if not isinstance(buses, list) or not buses:
        raise ValueError("buses must be a nonempty list")
    identities, seen = [], set()
    for bus in buses:
        bus = mapping(bus, "bus", {"id", "modules"})
        bus_id = integer(bus["id"], "bus.id", 0, 1)
        if bus_id in seen:
            raise ValueError(f"duplicate bus id {bus_id}")
        seen.add(bus_id)
        addresses = bus["modules"]
        if not isinstance(addresses, list):
            raise ValueError(f"bus {bus_id}.modules must be a list of integer I2C addresses")
        present = set()
        for address in addresses:
            integer(address, f"bus {bus_id} module address", 0x08, 0x77)
            if bus_id == 0 and address == 0x48:
                raise ValueError(
                    "bus 0 address 0x48 conflicts with QEMU's built-in temperature sensor"
                )
            if not 0x20 <= address <= 0x27:
                raise ValueError(f"PCF8575 address 0x{address:02x} must be in 0x20–0x27")
            if address in present:
                raise ValueError(f"duplicate module address 0x{address:02x} on bus {bus_id}")
            present.add(address)
            identities.append((bus_id, address))
    identities.sort()
    if not identities:
        raise ValueError("at least one physical module is required")
    start = document["start_angles"]
    if isinstance(start, list):
        if len(start) != len(identities):
            raise ValueError("start_angles must have one angle per module in bus/address order")
        angles = [integer(a, "start angle", 0, steps - 1) for a in start]
        resolved_seed = None
    elif start == "random" or isinstance(start, dict):
        if isinstance(start, dict):
            mapping(start, "start_angles", {"seed"})
            resolved_seed = integer(start["seed"], "start_angles.seed", 0, 0xFFFFFFFF)
        else:
            resolved_seed = secrets.randbits(32)
        angles = positions(resolved_seed, len(identities), steps)
    else:
        raise ValueError("start_angles must be random, {seed: integer}, or an angle list")
    if seed is not None:
        resolved_seed = integer(seed, "seed override", 0, 0xFFFFFFFF)
        angles = positions(resolved_seed, len(identities), steps)
    return {
        "schema_version": 1,
        "chip": "esp32",
        "seed": resolved_seed,
        "drum": dict(drum),
        "modules": [
            {"bus": b, "addr": a, "angle": angle}
            for (b, a), angle in zip(identities, angles, strict=True)
        ],
    }


def load_hardware(path, *, seed=None):
    path = Path(path)
    if not path.exists() and path.parent == Path("."):
        path = ROOT / "hardware" / f"{path.stem}.yaml"
    try:
        document = yaml.load(path.read_text(), Loader=HardwareLoader)
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"cannot load hardware {path}: {error}") from error
    return normalize_hardware(document, seed=seed)


def write_model_config(hardware, out: Path, *, trace=False):
    """Write normalized JSON with explicit output paths; pass only FLAPSIM_CONFIG to C."""
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    config = {
        **hardware,
        "outputs": {
            "state": str(out / "state.json"),
            "activity": str(out / "activity.json"),
            "timeline": str(out / "timeline.jsonl"),
            "trace": str(out / "i2c.log") if trace else None,
        },
    }
    path = out / "model-config.json"
    path.write_text(json.dumps(config, indent=2) + "\n")
    return path
