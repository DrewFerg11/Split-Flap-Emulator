"""Black-box firmware testing for Split-Flap Display."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("flapsim")
except PackageNotFoundError:
    __version__ = "0+uninstalled"
