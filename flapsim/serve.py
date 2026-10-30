"""Long-running home-lab mode (#21): one emulator with its control API and the live panel.

`flapsim serve` supervises a `flapsim run --scenario hold` child, so the run keeps its own
isolation, deadline handling and evidence, and serves the panel with controls for whichever run
is current. In QEMU mode the panel can load another factory image, uploaded or fetched from a
Split-Flap Display release; the supervisor stops the run and starts a fresh one with it.
"""

import hashlib
import os
import re
import signal
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

from flapsim.runner import FLASH_SIZE

# A merged ESP32 factory image starts with the bootloader at 0x1000, whose image header begins
# with the ESP image magic byte.
BOOTLOADER_OFFSET = 0x1000
IMAGE_MAGIC = 0xE9
RELEASE_TAG = re.compile(r"v\d{1,4}\.\d{1,4}\.\d{1,4}(?:-[0-9A-Za-z][0-9A-Za-z.]{0,31})?")
RELEASE_URL = (
    "https://github.com/DrewFerg11/Split-Flap-Display/releases/download/"
    "{tag}/splitflap-esp32_n4-{tag}-factory.bin"
)
DOWNLOAD_TIMEOUT = 60
STOP_TIMEOUT = 60


class ServeError(RuntimeError):
    """A firmware change the supervisor refused or could not complete."""


def check_image(data):
    """Reject anything that is not plausibly an ESP32 factory image for the 4 MiB flash."""
    if not BOOTLOADER_OFFSET < len(data) <= FLASH_SIZE:
        raise ServeError(f"a factory image is larger than 4 KiB and at most {FLASH_SIZE} bytes")
    if data[BOOTLOADER_OFFSET] != IMAGE_MAGIC:
        raise ServeError("not an ESP32 factory image (no bootloader at 0x1000)")


def download_release(tag):
    """The esp32_n4 factory image of a Split-Flap Display release."""
    if not isinstance(tag, str) or not RELEASE_TAG.fullmatch(tag):
        raise ServeError("a release tag looks like v1.4.1 or v1.5.0-rc.1")
    url = RELEASE_URL.format(tag=tag)
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response:
            data = response.read(FLASH_SIZE + 1)
    except OSError as error:
        raise ServeError(f"cannot download {tag}: {error}") from error
    return data


class Supervisor:
    """Owns the current `flapsim run --scenario hold` child and its output directory."""

    def __init__(self, out, run_arguments, mode, firmware=None):
        self.out = Path(out)
        self.run_arguments = list(run_arguments)
        self.mode = mode
        self.firmware = None
        self.firmware_info = None
        if firmware:
            self.use_firmware(Path(firmware), Path(firmware).name)
        self.number = last_run_number(self.out)
        self.process = None
        self.lock = threading.Lock()
        self.loading = False
        self.error = None
        self.stopped = False

    def use_firmware(self, path, name):
        self.firmware = path
        self.firmware_info = {
            "name": name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        }

    @property
    def run_dir(self):
        return self.out / f"run-{self.number}"

    def start(self):
        """Start the next run with the current firmware (the first one, or after a change)."""
        self.number += 1
        command = [sys.executable, "-m", "flapsim.cli", "run", *self.run_arguments]
        if self.mode == "qemu":
            command += ["--firmware", str(self.firmware)]
        command += [
            "--scenario",
            "hold",
            "--control-port",
            "0",
            "--i2c-history",
            "--out",
            str(self.run_dir),
        ]
        log = (self.out / f"run-{self.number}.log").open("wb")
        # Its own session: a terminal Ctrl-C reaches the supervisor, which stops the run once.
        self.process = subprocess.Popen(
            command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
        log.close()

    def stop_run(self):
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        process.send_signal(signal.SIGINT)
        try:
            process.wait(STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()

    def stop(self):
        with self.lock:
            self.stopped = True
            self.stop_run()

    def load(self, data, name):
        """Keep a new factory image and restart the run with it."""
        if self.mode != "qemu":
            raise ServeError("loading firmware needs QEMU mode; native mode builds from source")
        check_image(data)
        store = self.out / "firmware"
        store.mkdir(exist_ok=True)
        path = store / f"{hashlib.sha256(data).hexdigest()}.bin"
        path.write_bytes(data)
        return self.restart(lambda: self.use_firmware(path, name))

    def restart(self, change=None):
        """Stop the current run and start the next one, after an optional firmware change."""
        with self.lock:
            if self.stopped:
                raise ServeError("serve is stopping")
            self.loading = True
            try:
                self.stop_run()
                if change:
                    change()
                self.start()
                self.error = None
            except OSError as error:
                self.error = f"cannot restart the run: {error}"
                raise ServeError(self.error) from error
            finally:
                self.loading = False
        return self.describe()

    def describe(self):
        process = self.process
        return {
            "mode": self.mode,
            "run": self.run_dir.name,
            "running": process is not None and process.poll() is None,
            "exit_code": None if process is None else process.poll(),
            "firmware": self.firmware_info,
            "loading": self.loading,
            "error": self.error,
        }


def last_run_number(out):
    """The highest run-N (directory or log) already under out, so a restarted serve continues the
    numbering instead of truncating an earlier session's log or reusing its evidence directory."""
    numbers = [0]
    if out.is_dir():
        for entry in out.iterdir():
            match = re.fullmatch(r"run-(\d+)(?:\.log)?", entry.name)
            if match:
                numbers.append(int(match.group(1)))
    return max(numbers)


def serve(args, ready=print):
    """Run `flapsim serve` until interrupted (Ctrl-C, or SIGTERM from `docker stop`)."""
    from flapsim.panel import PanelServer

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    firmware = args.firmware
    if args.firmware_release:
        image = download_release(args.firmware_release)
        check_image(image)
        firmware = out / "firmware" / f"{hashlib.sha256(image).hexdigest()}.bin"
        firmware.parent.mkdir(exist_ok=True)
        firmware.write_bytes(image)
    elif args.mode == "qemu":
        check_image(Path(firmware).read_bytes())
    run_arguments = ["--mode", args.mode, "--hardware", args.hardware]
    for option, value in [
        ("--native-build", args.native_build),
        ("--firmware-src", args.firmware_src),
        ("--speed", args.speed),
        ("--timing", args.timing),
        ("--seed", args.seed),
        ("--qemu-prefix", args.qemu_prefix),
    ]:
        if value is not None:
            run_arguments += [option, str(value)]
    supervisor = Supervisor(out, run_arguments, args.mode, firmware)
    server = PanelServer(
        None, args.port, controls=True, supervisor=supervisor, bind=args.bind, hosts=args.allow_host
    )

    def stop(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        supervisor.start()
        ready(f"Serving {args.hardware} ({args.mode}) from {out}: {server.page_url}")
        for host in args.allow_host:
            ready(f"Also answering as http://{host}/#key={server.key}")
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous)
        server.server_close()
        supervisor.stop()
    return 0
