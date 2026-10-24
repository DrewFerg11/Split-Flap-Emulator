"""Strict physical-hardware configuration and a versioned model JSON boundary."""

import json
import secrets
from pathlib import Path

import yaml

from flapsim.faults import INSTANTS, KINDS, PARAMETERS
from flapsim.resources import resolve_resource

ROOT = Path(__file__).resolve().parents[1]
MECHANICAL_SALTS = {
    "steps_per_rotation": 0xA341316C,
    "magnet_position": 0xC8013EA4,
    "flap_offset": 0xAD90777D,
    "sensor_window": 0x7E95761E,
    "sensor_polarity": 0x9E3779B9,
}


FAULT_SALT = 0x5BD1E995
FAULT_KINDS = {name: choices - {"ok"} for name, choices in KINDS.items()}
# Startup YAML names parameters in seconds; resolved evidence records integer nanoseconds.
FAULT_PARAMETERS = {
    **{mode: (name,) for mode, name in PARAMETERS.items()},
    **{mode: tuple(name.removesuffix("_ns") for name in names) for mode, names in INSTANTS.items()},
}


def probability(value, name):
    if type(value) not in (int, float) or isinstance(value, bool) or not 0 < value <= 1:
        raise ValueError(f"{name} must be a probability in (0, 1]")
    return float(value)


def normalize_faults(value, where, seed):
    """Per-module injected faults (#19): at most one sensor, motor and I2C fault each."""
    startup = {name for names in FAULT_PARAMETERS.values() for name in names}
    mapping(value, f"{where} faults", set(), {*FAULT_KINDS, *startup})
    if not value.keys() & FAULT_KINDS.keys():
        raise ValueError(f"{where} faults must name a sensor, motor, i2c or power fault")
    normalized = {}
    for kind, choices in FAULT_KINDS.items():
        if kind in value:
            if type(value[kind]) is not str or value[kind] not in choices:
                raise ValueError(f"{where} faults.{kind} must be one of {sorted(choices)}")
            normalized[kind] = value[kind]
    wanted = {n for f in normalized.values() for n in FAULT_PARAMETERS.get(f, ())}
    given = value.keys() & startup
    if wanted != given:
        raise ValueError(
            f"{where} faults need exactly the parameters {sorted(wanted)}, got {sorted(given)}"
        )
    for name in sorted(given):
        if name.endswith("_at"):
            seconds = value[name]
            # Checked after rounding: the model needs a positive int64 nanosecond time.
            if (
                type(seconds) not in (int, float)
                or not 0 < seconds < 2**63 / 1_000_000_000
                or not 0 < round(seconds * 1_000_000_000) < 2**63
            ):
                raise ValueError(f"{where} faults.{name} must be guest seconds > 0")
            normalized[f"{name}_ns"] = round(seconds * 1_000_000_000)
        else:
            normalized[name] = probability(value[name], f"{where} faults.{name}")
    if normalized.get("power") == "drop" and (
        normalized["power_on_at_ns"] <= normalized["power_off_at_ns"]
    ):
        raise ValueError(f"{where} faults.power_on_at must be later than power_off_at")
    normalized["seed"] = seed
    return normalized


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


def mapping(value, name, keys, optional=()):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    unknown = value.keys() - keys - set(optional)
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


def mechanical_range(value, name, lower, upper):
    """An integer or a bounded, inclusive uniform distribution in full steps."""
    if isinstance(value, dict):
        mapping(value, name, {"mean", "spread"})
        mean = integer(value["mean"], f"{name}.mean", lower, upper)
        spread = integer(value["spread"], f"{name}.spread", 0, min(mean - lower, upper - mean))
        return mean, spread
    return integer(value, name, lower, upper), 0


def mechanical_spec(drum):
    ranges = {
        "steps_per_rotation": mechanical_range(
            drum["steps_per_rotation"], "drum.steps_per_rotation", 4, 1_000_000
        )
    }
    steps, spread = ranges["steps_per_rotation"]
    minimum = steps - spread
    for name, lower, upper in [
        ("magnet_position", 0, minimum - 1),
        ("flap_offset", 0, minimum - 1),
        ("sensor_window", 1, minimum),
    ]:
        ranges[name] = mechanical_range(drum[name], f"drum.{name}", lower, upper)
    polarity = drum["sensor_polarity"]
    if not isinstance(polarity, str) or polarity not in {"active-high", "active-low", "random"}:
        raise ValueError("sensor_polarity must be active-high, active-low, or random")
    return ranges, polarity


def normalize_hardware(document, *, seed=None):
    document = mapping(document, "hardware", {"chip", "buses", "drum", "start_angles"})
    if document["chip"] != "esp32":
        raise ValueError("chip must be esp32 (ESP32-C3/S3 are outside the MVP)")
    raw_drum = document["drum"]
    drum = mapping(
        {"flap_offset": 0, "sensor_polarity": "active-high", **raw_drum}
        if isinstance(raw_drum, dict)
        else raw_drum,
        "drum",
        {
            "steps_per_rotation",
            "flaps",
            "magnet_position",
            "sensor_window",
            "flap_offset",
            "sensor_polarity",
        },
    )
    defaults, default_polarity = mechanical_spec(drum)
    steps, steps_spread = defaults["steps_per_rotation"]
    min_steps = steps - steps_spread
    flaps = drum["flaps"]
    if (
        not isinstance(flaps, str)
        or not 2 <= len(flaps) <= min(95, min_steps)
        or len(set(flaps)) != len(flaps)
        or any(not 32 <= ord(c) <= 126 for c in flaps)
    ):
        raise ValueError(
            "drum.flaps must contain 2–95 unique printable ASCII characters, at most one per step"
        )
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
            raise ValueError(f"bus {bus_id}.modules must be a list of addresses or module mappings")
        present = set()
        for entry in addresses:
            overrides = {}
            address = entry
            if isinstance(entry, dict):
                mapping(entry, "module", {"addr"}, {*MECHANICAL_SALTS, "faults"})
                address = entry["addr"]
                overrides = {k: v for k, v in entry.items() if k != "addr"}
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
            identities.append((bus_id, address, overrides))
    identities.sort(key=lambda m: (m[0], m[1]))
    if not identities:
        raise ValueError("at least one physical module is required")
    start = document["start_angles"]
    if isinstance(start, list):
        if len(start) != len(identities):
            raise ValueError("start_angles must have one angle per module in bus/address order")
        angles = [integer(a, "start angle", 0, 999_999) for a in start]
        resolved_seed = None
    elif start == "random" or isinstance(start, dict):
        if isinstance(start, dict):
            mapping(start, "start_angles", {"seed"})
            resolved_seed = integer(start["seed"], "start_angles.seed", 0, 0xFFFFFFFF)
        else:
            resolved_seed = secrets.randbits(32)
    else:
        raise ValueError("start_angles must be random, {seed: integer}, or an angle list")
    if seed is not None:
        resolved_seed = integer(seed, "seed override", 0, 0xFFFFFFFF)
    # Separate streams keep starts and each mechanical parameter independent.
    mechanical_seed = resolved_seed if resolved_seed is not None else 0
    draws = {
        k: positions(mechanical_seed ^ salt, len(identities), 0x100000000)
        for k, salt in MECHANICAL_SALTS.items()
    }
    starts = (
        positions(resolved_seed, len(identities), 0x100000000)
        if resolved_seed is not None
        else None
    )
    modules = []
    for i, (bus_id, address, overrides) in enumerate(identities):
        overrides = dict(overrides)
        faults = overrides.pop("faults", None)
        try:
            ranges, polarity = mechanical_spec({**drum, **overrides})
        except ValueError as error:
            raise ValueError(f"module bus {bus_id} addr 0x{address:02x}: {error}") from error
        minimum = ranges["steps_per_rotation"][0] - ranges["steps_per_rotation"][1]
        if len(flaps) > minimum:
            raise ValueError("module flap count exceeds its smallest rotation")
        if isinstance(start, list):
            integer(angles[i], "start angle", 0, minimum - 1)
        geometry = {
            k: mean - spread + draws[k][i] % (2 * spread + 1)
            for k, (mean, spread) in ranges.items()
        }
        if polarity == "random":
            polarity = "active-low" if draws["sensor_polarity"][i] & 1 else "active-high"
        angle = starts[i] % geometry["steps_per_rotation"] if starts is not None else angles[i]
        module = {
            "bus": bus_id,
            "addr": address,
            "angle": angle,
            **geometry,
            "sensor_polarity": polarity,
        }
        if faults is not None:
            # A nonzero per-module stream, reproducible from the run seed.
            stream = (mechanical_seed ^ FAULT_SALT ^ (bus_id << 8 | address)) & 0xFFFFFFFF
            where = f"module bus {bus_id} addr 0x{address:02x}"
            module["faults"] = normalize_faults(faults, where, stream or FAULT_SALT)
        modules.append(module)
    return {
        "schema_version": 1,
        "chip": "esp32",
        "seed": resolved_seed,
        "drum": {
            **drum,
            **{k: mean for k, (mean, _) in defaults.items()},
            "sensor_polarity": "active-high" if default_polarity == "random" else default_polarity,
        },
        "modules": modules,
    }


def load_hardware(path, *, seed=None):
    try:
        document = yaml.load(resolve_resource("hardware", path).read_text(), Loader=HardwareLoader)
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
