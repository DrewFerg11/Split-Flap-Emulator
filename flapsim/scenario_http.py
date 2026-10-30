"""Bounded HTTP exchanges with the firmware's own web server."""

import http.client
import socket
import threading
import time
from urllib.parse import urlsplit

from flapsim.executor import ExecutionFailed


def http_batch(context, requests):
    """Run several HTTP exchanges at once: [(method, path, data)] -> [response or exception]."""
    address = urlsplit(context.endpoint)
    results = [None] * len(requests)
    connections, sockets = [], []

    def exchange(index, connection, method, path, data):
        try:
            connection.request(method, path, data, {"Content-Type": "application/json"})
            with connection.getresponse() as response:
                body = response.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024:
                    raise ExecutionFailed("HTTP response exceeds 1 MiB")
                results[index] = {"status": response.status, "body": body.decode(errors="replace")}
        except Exception as error:
            results[index] = error

    workers = []
    try:
        for index, (method, path, data) in enumerate(requests):
            context.checkpoint()
            connection = http.client.HTTPConnection(address.hostname, address.port)
            transport = socket.create_connection(
                (address.hostname, address.port),
                timeout=min(5, max(0.001, context.deadline - time.monotonic())),
            )
            transport.settimeout(max(0.001, context.deadline - time.monotonic()))
            connection.sock = transport
            connections.append(connection)
            sockets.append(transport)
            workers.append(
                threading.Thread(
                    target=exchange, args=(index, connection, method, path, data), daemon=True
                )
            )
        for worker in workers:
            worker.start()
        while any(worker.is_alive() for worker in workers):
            context.checkpoint()
            for worker in workers:
                worker.join(0.01)
        context.checkpoint()
        return results
    finally:
        for transport in sockets:
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            transport.close()
        for connection in connections:
            connection.close()


def http_request(context, method, path, data):
    """Supervise the whole HTTP exchange, including trickled headers/body and cancellation."""
    address = urlsplit(context.endpoint)
    connection = http.client.HTTPConnection(address.hostname, address.port)
    context.checkpoint()
    transport = socket.create_connection(
        (address.hostname, address.port),
        timeout=min(5, max(0.001, context.deadline - time.monotonic())),
    )
    transport.settimeout(max(0.001, context.deadline - time.monotonic()))
    connection.sock = transport
    results = []

    def exchange():
        try:
            connection.request(method, path, data, {"Content-Type": "application/json"})
            with connection.getresponse() as response:
                body = response.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024:
                    raise ExecutionFailed("HTTP response exceeds 1 MiB")
                results.append({"status": response.status, "body": body.decode(errors="replace")})
        except Exception as error:
            results.append(error)

    worker = threading.Thread(target=exchange, daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            context.checkpoint()
            worker.join(0.05)
        context.checkpoint()
        if isinstance(results[0], Exception):
            raise results[0]
        return results[0]
    finally:
        try:
            transport.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        transport.close()
        connection.close()
        worker.join(1)
        if worker.is_alive():
            raise RuntimeError("HTTP worker did not stop after socket shutdown")
