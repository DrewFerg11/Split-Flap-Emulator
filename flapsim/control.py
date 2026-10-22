"""Runner-owned control service; firmware HTTP remains a separate endpoint."""

import asyncio
import copy
import hmac
import json
import os
import secrets
import socket
import threading
import time
import uuid
from contextlib import contextmanager

from flapsim.state import MAX_CONTROL_ID, MECHANICS, validate_state

CAPABILITIES = {
    "snapshot": True,
    "set_mechanics": True,
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
        self.power = None
        connection.settimeout(0.5)

    def close(self):
        with self.lock:
            self.closed = True
            self.connection.close()

    def _validate_snapshot(self, state):
        validate_state(state, self.hardware, require_settled=False)
        if type(state.get("guest_resets")) is not int or state["guest_resets"] < 0:
            raise ValueError("invalid model reset count")
        for actual in state["modules"]:
            if (
                type(actual.get("coils")) is not int
                or actual["coils"] != (actual["latch"] >> 1) & 15
                or actual["energized"] != bool(actual["coils"])
            ):
                raise ValueError("invalid model coils")
        return state

    def _exchange(self, request):
        self.connection.sendall(request)
        data, _, flags, _ = self.connection.recvmsg(MAX_SNAPSHOT)
        if not data or flags & socket.MSG_TRUNC:
            raise ValueError("incomplete or oversized model reply")
        return json.loads(data)

    def snapshot(self):
        return self.dispatch()

    def view(self):
        return self.dispatch(with_power=True)

    def dispatch(self, command=None, journal=None, *, with_power=False):
        if not self.lock.acquire(blocking=False):
            raise Unavailable("model request already in progress")
        try:
            if self.closed:
                raise Unavailable("model channel is unavailable")
            if self.power is not None and self.power.state == "off":
                if command is not None:
                    return {"error": "controller_off"}
                frozen = copy.deepcopy(self.power.frozen)
                return (frozen, "off") if with_power else frozen
            stage = "intent_audit"
            try:
                if command is not None:
                    journal({"phase": "intent", "command": command})
                request = (
                    b"FLAPSIM_STATE/1"
                    if command is None
                    else (b"FLAPSIM_MECH/1 " + json.dumps(command, separators=(",", ":")).encode())
                )
                stage = "model_exchange"
                result = self._exchange(request)
                stage = "reply_validation"
                if command is not None and isinstance(result, dict) and set(result) == {"error"}:
                    if result["error"] not in {
                        "module_not_found",
                        "invalid_mechanics",
                        "revision_conflict",
                        "model_busy",
                        "stale_or_reused_id",
                        "revision_exhausted",
                        "backend_unavailable",
                        "controller_off",
                    }:
                        raise ValueError("invalid model rejection")
                else:
                    self._validate_snapshot(result)
                    if (
                        command is not None
                        and result.get("last_operation", {}).get("command") != command
                    ):
                        raise ValueError("model reply does not identify the command")
                if command is not None:
                    stage = "result_audit"
                    journal({"phase": "result", "command": command, "result": result})
                if with_power:
                    return result, self.power.state if self.power else "on"
                return result
            except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
                self.closed = True
                self.connection.close()
                if command is not None:
                    try:
                        journal(
                            {
                                "phase": "unknown",
                                "command": command,
                                "stage": stage,
                                "error": type(error).__name__,
                                "channel": "closed",
                            }
                        )
                    except (OSError, ValueError, TypeError, KeyError, RecursionError):
                        # The audit sink itself may be broken; never invent a durable outcome.
                        pass
                # An intent without a result is also unknown, never automatically retried.
                raise Unavailable("model reply unavailable") from error
        finally:
            self.lock.release()


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("non-finite JSON value")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


def validate_command(value, bus, addr):
    if not isinstance(value, dict) or set(value) != {"id", "expected_revision", "patch"}:
        raise ValueError("expected id, expected_revision and patch")
    for key, lo, hi in [("id", 1, MAX_CONTROL_ID), ("expected_revision", 0, MAX_CONTROL_ID - 1)]:
        if type(value[key]) is not int or not lo <= value[key] <= hi:
            raise ValueError("invalid " + key)
    patch = value["patch"]
    if not isinstance(patch, dict) or not patch or set(patch) - MECHANICS:
        raise ValueError("empty or unknown mechanics fields")
    for key, part in patch.items():
        if key == "sensor_polarity":
            if part not in ("active-high", "active-low"):
                raise ValueError("invalid sensor polarity")
        elif type(part) is not int or not 0 <= part <= 1000000:
            raise ValueError("mechanics require literal integers in range")
    return {**value, "bus": bus, "addr": addr}


class ControlServer:
    """Loopback only; auth secret lives in a mode-0600 file, never the manifest."""

    def __init__(self, port, out, hardware, *, backend="qemu"):
        if backend not in {"qemu", "native"}:
            raise ValueError("unknown control backend")
        check_port(port)
        import uvicorn
        from starlette.applications import Starlette
        from starlette.datastructures import Headers
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        self.hardware = hardware
        self.backend = backend
        self.client = None
        self.resets = None
        self.powers = None
        self.power_metadata = None
        self.capabilities = dict(CAPABILITIES)
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

            def envelope(snapshot, power_state=None):
                power_state = power_state or (self.powers.state if self.powers else "on")
                return {
                    "api_version": 1,
                    "run_id": self.run_id,
                    "backend": backend,
                    "boot_generation": snapshot["guest_resets"],
                    "live": power_state == "on",
                    "power_state": power_state,
                    "capabilities": dict(self.capabilities),
                }

            def journal(entry):
                with (out / "control.jsonl").open("a") as log:
                    log.write(
                        json.dumps(
                            {"api_version": 1, "run_id": self.run_id, **entry},
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    log.flush()
                    os.fsync(log.fileno())

            async def state(request):
                # SnapshotClient is blocking, bounded and single-flight. Starlette's
                # worker pool keeps a paused/busy model from blocking HTTP shutdown.
                from starlette.concurrency import run_in_threadpool

                client = self.client
                if client is None:
                    return JSONResponse({"error": "backend_unavailable"}, status_code=503)
                try:
                    snapshot, power_state = await run_in_threadpool(client.view)
                except Unavailable:
                    return JSONResponse({"error": "backend_unavailable"}, status_code=503)
                return JSONResponse({**envelope(snapshot, power_state), "state": snapshot})

            async def mechanics(request):
                from starlette.concurrency import run_in_threadpool
                from starlette.requests import ClientDisconnect

                async def read_body():
                    data = bytearray()
                    async for chunk in request.stream():
                        data.extend(chunk)
                        if len(data) > 2048:
                            return None
                    return bytes(data)

                if request.headers.getlist("content-type") != ["application/json"]:
                    return JSONResponse({"error": "json_required"}, status_code=415)
                try:
                    data = await asyncio.wait_for(read_body(), timeout=1)
                except (TimeoutError, ClientDisconnect):
                    return JSONResponse({"error": "body_timeout"}, status_code=408)
                if data is None:
                    return JSONResponse({"error": "body_too_large"}, status_code=413)
                try:
                    value = strict_json(data.decode("utf-8"))
                except (ValueError, UnicodeError, RecursionError):
                    return JSONResponse({"error": "invalid_json"}, status_code=400)
                bus, addr = request.path_params["bus"], request.path_params["addr"]
                if (bus, addr) not in {(m["bus"], m["addr"]) for m in hardware["modules"]}:
                    return JSONResponse({"error": "module_not_found"}, status_code=404)
                try:
                    command = validate_command(value, bus, addr)
                except ValueError:
                    return JSONResponse({"error": "invalid_command"}, status_code=422)
                client = self.client
                if client is None:
                    return JSONResponse({"error": "backend_unavailable"}, status_code=503)
                try:
                    result = await run_in_threadpool(client.dispatch, command, journal)
                except Unavailable:
                    return JSONResponse(
                        {"error": "result_unknown", "id": command["id"]}, status_code=503
                    )
                if "error" in result:
                    code = {
                        "module_not_found": 404,
                        "invalid_mechanics": 422,
                        "backend_unavailable": 503,
                    }.get(result["error"], 409)
                    return JSONResponse(result, status_code=code)
                return JSONResponse(
                    {
                        **envelope(result),
                        "status": "applied",
                        "operation": result["last_operation"],
                    },
                    status_code=202,
                )

            async def operation(request):
                from starlette.concurrency import run_in_threadpool

                client = self.client
                if client is None:
                    return JSONResponse({"error": "backend_unavailable"}, status_code=503)
                try:
                    snapshot, power_state = await run_in_threadpool(client.view)
                except Unavailable:
                    return JSONResponse({"error": "backend_unavailable"}, status_code=503)
                applied = snapshot.get("last_operation")
                if applied is None or applied["command"]["id"] != request.path_params["id"]:
                    return JSONResponse(
                        {**envelope(snapshot, power_state), "status": "unknown"}, status_code=404
                    )
                return JSONResponse(
                    {**envelope(snapshot, power_state), "status": "applied", "operation": applied}
                )

            async def reset(request):
                from starlette.concurrency import run_in_threadpool
                from starlette.requests import ClientDisconnect

                is_power = request.url.path.startswith("/api/emu/power")
                controller = self.powers if is_power else self.resets
                if controller is None:
                    return JSONResponse({"error": "backend_unavailable"}, status_code=503)
                if request.method == "GET":
                    receipt = controller.receipt
                    if receipt is None or receipt["command"]["id"] != request.path_params["id"]:
                        return JSONResponse(
                            {
                                "run_id": self.run_id,
                                "api_version": 1,
                                "backend": backend,
                                "status": "unknown",
                            },
                            status_code=404,
                        )
                    return JSONResponse(
                        {"run_id": self.run_id, "api_version": 1, "backend": backend, **receipt}
                    )
                if request.headers.getlist("content-type") != ["application/json"]:
                    return JSONResponse({"error": "json_required"}, status_code=415)

                async def read_body():
                    data = bytearray()
                    async for chunk in request.stream():
                        data.extend(chunk)
                        if len(data) > 2048:
                            return None
                    return bytes(data)

                try:
                    data = await asyncio.wait_for(read_body(), timeout=1)
                except (TimeoutError, ClientDisconnect):
                    return JSONResponse({"error": "body_timeout"}, status_code=408)
                if data is None:
                    return JSONResponse({"error": "body_too_large"}, status_code=413)
                try:
                    command = strict_json(data.decode("utf-8"))
                except (ValueError, UnicodeError, RecursionError):
                    return JSONResponse({"error": "invalid_json"}, status_code=400)
                if (
                    not isinstance(command, dict)
                    or set(command) != ({"id", "on"} if is_power else {"id"})
                    or type(command["id"]) is not int
                    or not 1 <= command["id"] <= MAX_CONTROL_ID
                    or (is_power and type(command["on"]) is not bool)
                ):
                    return JSONResponse({"error": "invalid_command"}, status_code=422)
                try:
                    result = await run_in_threadpool(controller.apply, command, journal)
                except Unavailable:
                    return JSONResponse(
                        {"error": "result_unknown", "id": command["id"]}, status_code=503
                    )
                code = 202
                if "error" in result:
                    code = (
                        409 if result["error"] in {"stale_or_reused_id", "controller_off"} else 503
                    )
                elif result["status"] == "unknown":
                    code = 503
                return JSONResponse(
                    {"run_id": self.run_id, "api_version": 1, "backend": backend, **result},
                    status_code=code,
                )

            app = Starlette(
                routes=[
                    Route("/api/emu/state", state, methods=["GET"]),
                    Route(
                        "/api/emu/modules/{bus:int}/{addr:int}/mech", mechanics, methods=["POST"]
                    ),
                    Route("/api/emu/operations/{id:int}", operation, methods=["GET"]),
                    Route("/api/emu/reset", reset, methods=["POST"]),
                    Route("/api/emu/reset/operations/{id:int}", reset, methods=["GET"]),
                    Route("/api/emu/power", reset, methods=["POST"]),
                    Route("/api/emu/power/operations/{id:int}", reset, methods=["GET"]),
                ]
            )

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

    def attach(self, connection, *, reset=None, power=None):
        self.detach()
        self.client = SnapshotClient(connection, self.hardware)
        if self.backend == "native" or reset is not None:
            from flapsim.lifecycle import ResetController

            self.resets = ResetController(self.client, reset)
            self.capabilities["reset"] = True
        if power is not None:
            from flapsim.power import PowerController

            self.powers = PowerController(self.client, power)
            self.capabilities["power"] = True

    def detach(self):
        client, self.client = self.client, None
        power, self.powers = self.powers, None
        self.resets = None
        self.capabilities["reset"] = False
        self.capabilities["power"] = False
        if client is not None:
            client.close()
        if power is not None:
            self.power_metadata = power.metadata()

    def metadata(self):
        return {
            "api_version": 1,
            "run_id": self.run_id,
            "endpoint": self.endpoint,
            "token_file": str(self.token_path),
            "capabilities": dict(self.capabilities),
            **(
                {"power": self.powers.metadata()}
                if self.powers
                else {"power": self.power_metadata}
                if self.power_metadata
                else {}
            ),
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
