"""Runner-owned control service; firmware HTTP remains a separate endpoint.

Command schemas live in control_commands and the private model channel in control_channel.
"""

import asyncio
import hmac
import json
import os
import secrets
import socket
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext

from flapsim.control_channel import SnapshotClient, Unavailable
from flapsim.control_commands import strict_json, validate_command, validate_operation
from flapsim.faults import FAULT_SUPPORT

CAPABILITIES = {
    "snapshot": True,
    "set_mechanics": True,
    "set_fault": True,
    "reset": False,
    "power": False,
    "speed": False,
    "i2c_history": False,
    "stream": False,
}


def check_port(port, *, telemetry=False):
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("--control-port must be an integer in [0, 65535]")
    try:
        import starlette  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError as error:
        raise ValueError(
            "control API requires the optional dependencies: pip install 'flapsim[control]'"
        ) from error

    if telemetry:
        try:
            from uvicorn.protocols.websockets.websockets_sansio_impl import (  # noqa: F401
                WebSocketsSansIOProtocol,
            )
        except ImportError as error:
            raise ValueError(
                "--i2c-history requires WebSocket dependencies: pip install 'flapsim[control]'"
            ) from error


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


class ControlServer:
    """Shared dispatcher with an optional loopback HTTP API (port=None is private)."""

    def __init__(
        self, port, out, hardware, *, backend="qemu", speed=5.0, timing="ci", telemetry=False
    ):
        if backend not in {"qemu", "native"}:
            raise ValueError("unknown control backend")
        self.hardware = hardware
        self.backend = backend
        self.client = None
        self.resets = None
        self.powers = None
        self.power_metadata = None
        self.channel_failure = None
        self.channel_latency = None
        self.speeds = None
        self.initial_speed = speed
        self.speed_metadata = None
        self.timing = timing
        self.capabilities = dict(CAPABILITIES)
        self.run_id = str(uuid.uuid4())
        self.token = None
        self.endpoint = self.token_path = None
        self.out = out
        self.journal = self.audit
        self.telemetry = None
        self.subscribers = 0
        self.listener = None
        self.thread = None
        if port is None:
            if telemetry:
                raise ValueError("structured telemetry requires --control-port")
            return
        check_port(port, telemetry=telemetry)
        import uvicorn
        from starlette.applications import Starlette
        from starlette.datastructures import Headers
        from starlette.responses import JSONResponse
        from starlette.routing import Route, WebSocketRoute

        self.token = secrets.token_urlsafe(32)
        self.listener = socket.socket()
        try:
            if telemetry:
                from flapsim.telemetry import Collector

                self.telemetry = Collector(out, self.run_id, hardware)
                self.capabilities.update(i2c_history=True, stream=True)
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
                    "fault_support": FAULT_SUPPORT,
                    "speed": self.clock_metadata(),
                }

            journal = self.journal

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
                    command = validate_operation("mechanics", value, hardware, bus=bus, addr=addr)
                except ValueError:
                    return JSONResponse({"error": "invalid_command"}, status_code=422)
                client = self.client
                if client is None:
                    return JSONResponse({"error": "backend_unavailable"}, status_code=503)
                try:
                    result = await run_in_threadpool(
                        self.dispatch, "mechanics", value, bus=bus, addr=addr
                    )
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
                is_speed = request.url.path.startswith("/api/emu/speed")
                if is_speed and backend == "qemu":
                    if request.method == "GET" and "id" not in request.path_params:
                        return JSONResponse(self.clock_metadata())
                    return JSONResponse(
                        {
                            "error": "unsupported_capability",
                            "reason": "QEMU timing is fixed at startup",
                            "speed": self.clock_metadata(),
                        },
                        status_code=409,
                    )
                controller = self.speeds if is_speed else self.powers if is_power else self.resets
                if controller is None:
                    return JSONResponse({"error": "backend_unavailable"}, status_code=503)
                if request.method == "GET" and is_speed and "id" not in request.path_params:
                    try:
                        result = await run_in_threadpool(controller.apply, None, journal)
                    except Unavailable:
                        return JSONResponse({"error": "backend_unavailable"}, status_code=503)
                    return JSONResponse(result, status_code=503 if "error" in result else 200)
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
                kind = "speed" if is_speed else "power" if is_power else "reset"
                try:
                    validate_operation(kind, command, hardware)
                except ValueError:
                    return JSONResponse({"error": "invalid_command"}, status_code=422)
                try:
                    result = await run_in_threadpool(self.dispatch, kind, command)
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

            def history_page(params):
                from flapsim.telemetry import CursorError

                if set(params) - {"since", "limit"} or any(
                    len(params.getlist(key)) != 1 for key in params
                ):
                    raise CursorError("invalid_query")
                limit = params.get("limit", "128")
                if not limit.isascii() or not limit.isdecimal() or len(limit) > 3:
                    raise CursorError("invalid_limit")
                return self.telemetry.history.page(params.get("since"), int(limit))

            async def i2c(request):
                from flapsim.telemetry import CursorError

                if self.telemetry is None:
                    return JSONResponse({"error": "unsupported_capability"}, status_code=409)
                try:
                    page = history_page(request.query_params)
                except CursorError as error:
                    return JSONResponse({"error": str(error)}, status_code=error.status)
                return JSONResponse(page, status_code=503 if page["error"] else 200)

            async def stream(websocket):
                from starlette.websockets import WebSocketDisconnect

                from flapsim.telemetry import CursorError

                if self.telemetry is None or self.subscribers >= 8:
                    await websocket.close(code=1013)
                    return
                self.subscribers += 1
                try:
                    await websocket.accept()
                    authorization = websocket.headers.getlist("authorization")
                    if not authorization:
                        try:
                            message = await asyncio.wait_for(websocket.receive(), 1)
                            if message["type"] == "websocket.disconnect":
                                return
                            if "text" not in message or len(message["text"].encode()) > 2048:
                                raise ValueError("invalid authentication message")
                            value = strict_json(message["text"])
                        except (TimeoutError, ValueError, RecursionError):
                            await websocket.close(code=1008)
                            return
                        if (
                            not isinstance(value, dict)
                            or set(value) != {"token"}
                            or not isinstance(value["token"], str)
                            or not hmac.compare_digest(value["token"].encode(), self.token.encode())
                        ):
                            await websocket.close(code=1008)
                            return
                    try:
                        page = history_page(websocket.query_params)
                    except CursorError as error:
                        await websocket.send_json({"error": str(error)})
                        await websocket.close(code=1008)
                        return
                    since = None
                    while True:
                        if since is not None:
                            page = self.telemetry.history.page(since)
                        if page["records"] or page["gap"] or since is None or page["error"]:
                            await asyncio.wait_for(websocket.send_json(page), 1)
                        since = page["next"]
                        if page["stopped"] or page["error"]:
                            await websocket.close(code=1011 if page["error"] else 1000)
                            return
                        # One bounded page per turn. Receive also observes disconnects;
                        # post-auth application messages are not part of this protocol.
                        try:
                            message = await asyncio.wait_for(
                                websocket.receive(), 0.001 if page["more"] else 0.05
                            )
                        except TimeoutError:
                            continue
                        if message["type"] == "websocket.disconnect":
                            return
                        await websocket.close(code=1008)
                        return
                except (WebSocketDisconnect, TimeoutError, OSError):
                    pass
                finally:
                    self.subscribers -= 1

            app = Starlette(
                routes=[
                    Route("/api/emu/state", state, methods=["GET"]),
                    Route("/api/emu/i2c", i2c, methods=["GET"]),
                    WebSocketRoute("/api/emu/stream", stream),
                    Route(
                        "/api/emu/modules/{bus:int}/{addr:int}/mech", mechanics, methods=["POST"]
                    ),
                    Route("/api/emu/operations/{id:int}", operation, methods=["GET"]),
                    Route("/api/emu/reset", reset, methods=["POST"]),
                    Route("/api/emu/reset/operations/{id:int}", reset, methods=["GET"]),
                    Route("/api/emu/speed", reset, methods=["GET", "POST"]),
                    Route("/api/emu/speed/operations/{id:int}", reset, methods=["GET"]),
                    Route("/api/emu/power", reset, methods=["POST"]),
                    Route("/api/emu/power/operations/{id:int}", reset, methods=["GET"]),
                ]
            )

            async def boundary(scope, receive, send):
                headers = Headers(scope=scope)
                if scope["type"] == "websocket":
                    authorization = headers.getlist("authorization")
                    if not accepts_authority(
                        headers.getlist("host"), headers.getlist("origin"), self.port
                    ) or (
                        authorization
                        and (
                            len(authorization) != 1
                            or not hmac.compare_digest(
                                authorization[0].encode(), ("Bearer " + self.token).encode()
                            )
                        )
                    ):
                        await send({"type": "websocket.close", "code": 1008})
                        return
                    await app(scope, receive, send)
                    return
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
                    ws="websockets-sansio" if telemetry else "none",
                    ws_max_size=2048,
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

    def audit(self, entry):
        with (self.out / "control.jsonl").open("a") as log:
            log.write(
                json.dumps(
                    {"api_version": 1, "run_id": self.run_id, **entry}, separators=(",", ":")
                )
                + "\n"
            )
            log.flush()
            os.fsync(log.fileno())
        if self.telemetry:
            self.telemetry.audit(entry)

    def dispatch(self, kind, value, *, bus=None, addr=None):
        """Validate once, then use the same single-flight owner and durable audit."""
        command = validate_operation(kind, value, self.hardware, bus=bus, addr=addr)
        if kind == "speed" and self.backend == "qemu":
            return {"error": "unsupported_capability", "reason": "QEMU timing is fixed at startup"}
        client = self.client
        if client is None:
            return {"error": "backend_unavailable"}
        if kind == "mechanics":
            return client.dispatch(command, self.journal)
        controller = {"reset": self.resets, "power": self.powers, "speed": self.speeds}[kind]
        if controller is None:
            return {"error": "backend_unavailable"}
        return controller.apply(command, self.journal)

    def attach(self, connection, *, reset=None, power=None):
        self.detach()
        self.client = SnapshotClient(connection, self.hardware)
        if self.backend == "native" or reset is not None:
            from flapsim.lifecycle import ResetController

            self.resets = ResetController(self.client, reset)
            self.capabilities["reset"] = True
        if self.backend == "native":
            from flapsim.speed import SpeedController

            self.speeds = SpeedController(self.client, self.initial_speed)
            self.capabilities["speed"] = True
        if power is not None:
            from flapsim.power import PowerController

            self.powers = PowerController(self.client, power)
            self.capabilities["power"] = True

    def detach(self):
        client, self.client = self.client, None
        power, self.powers = self.powers, None
        speeds, self.speeds = self.speeds, None
        self.capabilities["speed"] = False
        self.resets = None
        self.capabilities["reset"] = False
        self.capabilities["power"] = False
        if client is not None:
            client.close()
            self.channel_failure = client.failure
            self.channel_latency = client.latency()
        if power is not None:
            self.power_metadata = power.metadata()
        if speeds is not None:
            self.speed_metadata = speeds.metadata()

    def clock_metadata(self):
        if self.backend == "qemu":
            return {
                "runtime": False,
                "timing": self.timing,
                "reason": "QEMU timing is fixed at startup",
            }
        return (
            self.speeds.metadata()
            if self.speeds
            else self.speed_metadata
            or {"current": self.initial_speed, "min": 0.25, "max": 50, "runtime": False}
        )

    def exchange_latency(self):
        client = self.client
        return (client.latency() if client else None) or self.channel_latency

    def metadata(self):
        return {
            "api_version": 1,
            "run_id": self.run_id,
            "endpoint": self.endpoint,
            **({"token_file": str(self.token_path)} if self.token_path else {}),
            **({"channel_failure": self.channel_failure} if self.channel_failure else {}),
            **({"channel_latency": latency} if (latency := self.exchange_latency()) else {}),
            "fault_support": FAULT_SUPPORT,
            **({"telemetry": self.telemetry.metadata()} if self.telemetry else {}),
            "speed": self.clock_metadata(),
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
            if self.listener is not None:
                self.listener.close()
        if self.telemetry:
            self.telemetry.close()
        if self.thread is not None and self.thread.is_alive():
            raise RuntimeError("control HTTP server did not stop")


@contextmanager
def channel(service):
    """A new private channel for each startup attempt; inherited only by its child."""
    if service is None:
        yield {}, (), None, None
        return
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    transport = service.telemetry.channel() if service.telemetry else nullcontext(({}, ()))
    with parent, child, transport as (extra_env, extra_fds):
        try:
            yield (
                {"FLAPSIM_CONTROL_FD": str(child.fileno()), **extra_env},
                (child.fileno(), *extra_fds),
                parent,
                child,
            )
        finally:
            service.detach()


# Callers and tests import these from here.
__all__ = [
    "ControlServer",
    "SnapshotClient",
    "Unavailable",
    "accepts_authority",
    "channel",
    "check_port",
    "strict_json",
    "validate_command",
    "validate_operation",
]
