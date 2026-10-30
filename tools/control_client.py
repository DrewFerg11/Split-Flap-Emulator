"""Authenticated loopback calls to a running probe's control API, shared by the verify tools."""

import json
import urllib.error
import urllib.request


class ControlClient:
    """The run's control endpoint and token, from run.json and control-token in its output."""

    def __init__(self, context, timeout):
        manifest = json.loads((context.out / "run.json").read_text())
        self.context = context
        self.endpoint = manifest["control"]["endpoint"]
        self.token = (context.out / "control-token").read_text().strip()
        self.timeout = timeout
        # Loopback only: never route through a configured HTTP proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, path, body=None):
        """(HTTP status, decoded JSON) for GET, or POST of `body`, at an absolute API path."""
        self.context.checkpoint()
        request = urllib.request.Request(
            self.endpoint + path,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
            data=None if body is None else json.dumps(body).encode(),
        )
        try:
            response = self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def api(self, path="/state", body=None, expected=200):
        """An /api/emu call that must answer `expected`."""
        status, result = self.call("/api/emu" + path, body)
        if status != expected:
            raise RuntimeError(f"{path}: HTTP {status}: {result}")
        return result
