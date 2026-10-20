"""Resolve explicit user files or installed built-in YAML resources."""

from importlib.resources import files
from pathlib import Path


def resolve_resource(kind, value):
    path = Path(value)
    if path.is_file():
        return path
    if path.parent == Path(".") and path.suffix in {"", ".yaml"}:
        resource = files("flapsim").joinpath("data", kind, f"{path.stem}.yaml")
        if resource.is_file():
            return resource
    raise FileNotFoundError(f"no {kind} file or built-in named {value!s}")
