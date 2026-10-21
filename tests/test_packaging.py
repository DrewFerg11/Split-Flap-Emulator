"""Installed-resource behavior and publication guards before any registry mutation."""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from flapsim.executor import load_executor
from flapsim.hardware import load_hardware
from flapsim.resources import resolve_resource
from tools.publish_image import (
    REQUIRED_CASES,
    Registry,
    promote,
    release_version,
    validate_checkout,
)
from tools.verify_boot import GOLDEN
from tools.verify_container import ImageVerifier
from tools.verify_publication_gate import validate_workflows

REVISION = "a" * 40
IMAGE_ID = "sha256:" + "b" * 64
DIGEST = "sha256:" + "c" * 64


def test_container_golden_baseline_is_explicit_and_rejection_stays_strict():
    verifier = ImageVerifier.__new__(ImageVerifier)
    golden = verifier.command("esp32-8-single", "release-smoke-8", known_issues=True)
    assert golden[golden.index("--known-issues") + 1] == "/known-issues.yaml"
    wrong = verifier.command("esp32-8-single", "version-check", wrong_version=True)
    assert "--known-issues" not in wrong
    assert "--known-issues" not in verifier.command("esp32-8-single", "boot", timeout=3)
    with pytest.raises(ValueError):
        verifier.command("esp32-8-single", "version-check", wrong_version=True, known_issues=True)


def candidate():
    info = {
        "Id": IMAGE_ID,
        "Architecture": "amd64",
        "Os": "linux",
        "Config": {
            "Labels": {
                "org.opencontainers.image.revision": REVISION,
                "org.opencontainers.image.version": "1.2.3",
            }
        },
    }
    proof = {
        "passed": True,
        "mode": "all",
        "timeout_scale": 1,
        "image": {"Id": IMAGE_ID},
        "resources": {"version": "1.2.3", "provenance": {"source_revision": REVISION}},
        "cases": [
            {"case": name, "exit_code": code, "expected_exit": code}
            for name, code in REQUIRED_CASES.items()
        ],
    }
    return proof, info


class ControlledRegistry:
    def __init__(self, *, existing=False, newer=False):
        self.pushed = {"1.2.3"} if existing else set()
        self.commands, self.checks = [], []
        self.newer = newer

    def digest(self, tag):
        self.checks.append(tag)
        return DIGEST if tag in self.pushed else None

    def tags(self):
        return list(self.pushed) + (["2.0.0"] if self.newer else [])

    def docker(self, command):
        self.commands.append(command)
        if command[0] == "push":
            self.pushed.add(command[1].rsplit(":", 1)[1])


def test_builtins_resolve_outside_checkout_and_explicit_bad_paths_do_not_fall_back(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    assert len(load_hardware("esp32-8-single")["modules"]) == 8
    assert len(load_hardware("esp32-16-dual.yaml")["modules"]) == 16
    executor = load_executor(
        "release-smoke-8",
        load_hardware("esp32-8-single"),
        variables=["FIRMWARE_VERSION=v1.4.1"],
        firmware_sha256=GOLDEN["sha256"],
    )
    assert executor.document["name"] == "release-smoke-8"
    with pytest.raises(FileNotFoundError):
        resolve_resource("hardware", "missing/esp32-8-single.yaml")


@pytest.mark.parametrize(
    "tag,version,stable",
    [
        ("v1.2.3", "1.2.3", True),
        ("v1.2.3-rc.1", "1.2.3rc1", False),
        ("v0.1.0-dev.0", "0.1.0.dev0", False),
    ],
)
def test_release_version_policy(tag, version, stable):
    assert release_version(tag, version) == (tag[1:], stable)


@pytest.mark.parametrize(
    "tag", ["v1.2.4", "latest", "v1.2.3+metadata", "v01.2.3", "v1.2.3; echo bad"]
)
def test_wrong_or_unsupported_release_tags_are_rejected(tag):
    with pytest.raises(ValueError):
        release_version(tag, "1.2.3")


@pytest.mark.parametrize(
    "mutation", ["failed", "partial", "scale", "source", "image", "case", "version"]
)
def test_unverified_or_mismatched_candidates_cannot_reach_registry(mutation):
    proof, info = candidate()
    if mutation == "failed":
        proof["passed"] = False
    elif mutation == "partial":
        proof["mode"] = "lifecycle"
    elif mutation == "scale":
        proof["timeout_scale"] = 8
    elif mutation == "source":
        proof["resources"]["provenance"]["source_revision"] = "d" * 40
    elif mutation == "image":
        info["Id"] = "sha256:" + "d" * 64
    elif mutation == "case":
        proof["cases"][0]["exit_code"] = 5
    else:
        info["Config"]["Labels"]["org.opencontainers.image.version"] = "1.2.4"
    registry = ControlledRegistry()
    with pytest.raises(ValueError):
        promote(proof, info, REVISION, "1.2.3", True, registry, registry.docker)
    assert not registry.commands and not registry.checks


def test_existing_immutable_version_refuses_overwrite():
    registry = ControlledRegistry(existing=True)
    with pytest.raises(RuntimeError, match="already exists"):
        promote(*candidate(), REVISION, "1.2.3", True, registry, registry.docker)
    assert not registry.commands


@pytest.mark.parametrize(
    "stable,newer,latest", [(True, False, True), (False, False, False), (True, True, False)]
)
def test_verified_same_image_is_tagged_and_pushed_without_rebuild(stable, newer, latest):
    registry = ControlledRegistry(newer=newer)
    result = promote(*candidate(), REVISION, "1.2.3", stable, registry, registry.docker)
    assert result["reference"].endswith("@" + DIGEST)
    assert result["latest_promoted"] is latest
    assert {command[0] for command in registry.commands} == {"tag", "push"}
    assert all(command[1] == IMAGE_ID for command in registry.commands if command[0] == "tag")
    assert len(registry.commands) == (4 if latest else 2)


@pytest.fixture
def registry_server():
    state = {"status": 404, "redirect": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            value = (
                {"token": "fixture-registry-token"}
                if self.path.startswith("/token?")
                else {"tags": ["1.2.3"]}
            )
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self):
            self.send_response(state["status"])
            if state["redirect"]:
                self.send_header("Location", "http://invalid.example/")
            if state["status"] == 200:
                self.send_header("Docker-Content-Digest", DIGEST)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_registry_only_treats_authorized_404_as_missing(registry_server):
    url, state = registry_server
    registry = Registry("fixture-user", "fixture-token", base_url=url)
    assert registry.digest("1.2.3") is None
    state["status"] = 200
    assert registry.digest("1.2.3") == DIGEST
    assert registry.tags() == ["1.2.3"]
    for status in [401, 403, 429, 500, 302]:
        state.update(status=status, redirect=status == 302)
        with pytest.raises(RuntimeError, match=f"HTTP {status}"):
            registry.digest("1.2.3")


def test_publication_workflow_requires_verification_and_scopes_write_permission():
    validate_workflows(Path(__file__).resolve().parents[1])


def test_only_clean_checked_out_commits_on_main_can_be_published(tmp_path, monkeypatch):
    def git(*arguments):
        return subprocess.check_output(["git", *arguments], cwd=tmp_path, text=True).strip()

    git("init", "-b", "main")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
    git("add", "pyproject.toml")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "main",
    )
    revision = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", revision)
    monkeypatch.chdir(tmp_path)
    assert validate_checkout(revision, "v1.2.3") == ("1.2.3", True)
    (tmp_path / "uncommitted").write_text("change")
    with pytest.raises(ValueError, match="uncommitted"):
        validate_checkout(revision, "v1.2.3")
    (tmp_path / "uncommitted").unlink()
    git("checkout", "-b", "unmerged")
    (tmp_path / "change").write_text("unmerged")
    git("add", "change")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "unmerged",
    )
    with pytest.raises(subprocess.CalledProcessError):
        validate_checkout(git("rev-parse", "HEAD"), "v1.2.3")
