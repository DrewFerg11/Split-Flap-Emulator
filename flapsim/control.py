"""Runner-owned read-only control service; firmware HTTP remains a separate endpoint."""

import hmac
import json
import os
import secrets
import socket
import threading
import time
import uuid
from contextlib import contextmanager

from flapsim.state import validate_state

CAPABILITIES = {
    "snapshot": True,
    "set_mechanics": False,
    "set_fault": False,
    "reset": False,
    "power": False,
    "speed": False,
    "i2c_history": False,
    "stream": False,
}
MAX_SNAPSHOT = 64 * 1024


def check_port(port):
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("--control-port must be an integer in [0, 65535]")
    try:
        import starlette  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError as error:
        raise ValueError(
            "control API requires the optional dependencies: pip install 'flapsim[control]'"
        ) from error


class Unavailable(RuntimeError):
    pass


def accepts_authority(hosts, origins, port):
    """Accept equivalent HTTP default-port authorities, never foreign/duplicate headers."""
    allowed = {f"127.0.0.1:{port}"}
    if port == 80:
        allowed.add("127.0.0.1")
    return (
        len(hosts) == 1
        and hosts[0] in allowed
        and (not origins or (len(origins) == 1 and origins[0] in {"http://" + h for h in allowed}))
    )


class SnapshotClient:
    """One request in flight. A timeout poisons the channel so replies cannot be misattributed."""

    def __init__(self, connection, hardware):
        self.connection = connection
        self.hardware = hardware
        self.lock = threading.Lock()
        self.closed = False
        connection.settimeout(0.5)

    def close(self):
        with self.lock:
            self.closed = True
            self.connection.close()

    def snapshot(self):
        if not self.lock.acquire(blocking=False):
            raise Unavailable("snapshot request already in progress")
        try:
            if self.closed:
                raise Unavailable("model channel is unavailable")
            try:
                self.connection.sendall(b"FLAPSIM_STATE/1")
                data, _, flags, _ = self.connection.recvmsg(MAX_SNAPSHOT)
                if not data or flags & socket.MSG_TRUNC:
                    raise ValueError("incomplete or oversized model snapshot")
                state = json.loads(data)
                validate_state(state, self.hardware, require_settled=False)
                if type(state.get("guest_resets")) is not int or state["guest_resets"] < 0:
                    raise ValueError("invalid model reset count")
                for actual, expected in zip(
                    state["modules"], self.hardware["modules"], strict=True
                ):
                    if (
                        type(actual.get("coils")) is not int
                        or actual["coils"] != (actual["latch"] >> 1) & 15
                    ):
                        raise ValueError("invalid model coils")
                    if actual["energized"] != bool(actual["coils"]):
                        raise ValueError("model energized flag does not match coils")
                    mechanics = {
                        name: expected.get(name, self.hardware["drum"].get(name, default))
                        for name, default in [
                            ("steps_per_rotation", None),
                            ("magnet_position", None),
                            ("flap_offset", 0),
                            ("sensor_window", None),
                            ("sensor_polarity", "active-high"),
                        ]
                    }
                    if actual.get("mechanics") != mechanics:
                        raise ValueError("model mechanics do not match configured hardware")
                return state
            except (OSError, ValueError, TypeError, KeyError) as error:
                self.closed = True
                self.connection.close()
                raise Unavailable("model snapshot unavailable") from error
        finally:
            self.lock.release()


class ControlServer:
    """Loopback only; auth secret lives in a mode-0600 file, never the manifest."""

    def __init__(self, port, out, hardware):
        check_port(port)
        import uvicorn
        from starlette.applications import Starlette
        from starlette.datastructures import Headers
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        self.hardware = hardware
        self.client = None
        self.run_id = str(uuid.uuid4())
        self.token = secrets.token_urlsafe(32)
        self.listener = socket.socket()
        self.thread = None
        try:
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listener.bind(("127.0.0.1", port))
            self.listener.listen(16)
            self.port = self.listener.getsockname()[1]
            self.endpoint = f"http://127.0.0.1:{self.port}"
            self.token_path = out / "control-token"
            fd = os.open(self.token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as secret:
                secret.write(self.token + "\n")

            async def state(request):
                # SnapshotClient is blocking, bounded and single-flight. Starlette's
                # worker pool keeps a paused/busy model from blocking HTTP shutdown.
                from starlette.concurrency import run_in_threadpool

                client = self.client
                if client is None:
                    return JSONResponse({"error": "backend_unavailable"}, status_code=503)
                try:
                    snapshot = await run_in_threadpool(client.snapshot)
                except Unavailable:
                    return JSONResponse({"error": "backend_unavailable"}, status_code=503)
                return JSONResponse(
                    {
                        "api_version": 1,
                        "run_id": self.run_id,
                        "backend": "qemu",
                        "boot_generation": snapshot.pop("guest_resets"),
                        "live": True,
                        "capabilities": CAPABILITIES,
                        "state": snapshot,
                    }
                )

            app = Starlette(routes=[Route("/api/emu/state", state, methods=["GET"])])

            async def boundary(scope, receive, send):
                headers = Headers(scope=scope)
                if not accepts_authority(
                    headers.getlist("host"), headers.getlist("origin"), self.port
                ):
                    response = JSONResponse({"error": "forbidden_origin_or_host"}, status_code=403)
                elif not hmac.compare_digest(
                    headers.get("authorization", "").encode("utf-8"),
                    ("Bearer " + self.token).encode("ascii"),
                ):
                    response = JSONResponse({"error": "unauthorized"}, status_code=401)
                else:

                    async def no_cache(message):
                        if message["type"] == "http.response.start":
                            message["headers"].append((b"cache-control", b"no-store"))
                        await send(message)

                    await app(scope, receive, no_cache)
                    return
                response.headers["Cache-Control"] = "no-store"
                await response(scope, receive, send)

            self.server = uvicorn.Server(
                uvicorn.Config(
                    boundary,
                    access_log=False,
                    log_level="error",
                    proxy_headers=False,
                    server_header=False,
                    lifespan="off",
                    ws="none",
                    http="h11",
                    timeout_keep_alive=1,
                    timeout_graceful_shutdown=1,
                    limit_concurrency=16,
                    h11_max_incomplete_event_size=16 * 1024,
                )
            )
            self.thread = threading.Thread(
                target=self.server.run, kwargs={"sockets": [self.listener]}
            )
            self.thread.start()
            deadline = time.monotonic() + 3
            while not self.server.started:
                if not self.thread.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError("control HTTP server did not start")
                time.sleep(0.01)
        except BaseException:
            self.close()
            raise

    def attach(self, connection):
        self.detach()
        self.client = SnapshotClient(connection, self.hardware)

    def detach(self):
        client, self.client = self.client, None
        if client is not None:
            client.close()

    def metadata(self):
        return {
            "api_version": 1,
            "run_id": self.run_id,
            "endpoint": self.endpoint,
            "token_file": str(self.token_path),
            "capabilities": dict(CAPABILITIES),
        }

    def close(self):
        self.detach()
        try:
            if self.thread is not None and self.thread.ident is not None:
                self.server.should_exit = True
                self.thread.join(4)
        finally:
            self.listener.close()
        if self.thread is not None and self.thread.is_alive():
            raise RuntimeError("control HTTP server did not stop")


@contextmanager
def channel(service):
    """A new private channel for each startup attempt; inherited only by its child."""
    if service is None:
        yield {}, (), None, None
        return
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with parent, child:
        try:
            yield {"FLAPSIM_CONTROL_FD": str(child.fileno())}, (child.fileno(),), parent, child
        finally:
            service.detach()
