"""Installed-resource behavior and publication guards before any registry mutation."""

import argparse
import contextlib
import hashlib
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

import tools.verify_container as verify_container
from flapsim.executor import load_executor
from flapsim.hardware import load_hardware
from flapsim.resources import resolve_resource
from tools.publish_image import (
    ARCHITECTURES,
    IMAGE,
    REQUIRED_CASES,
    Registry,
    load_candidate,
    promote,
    release_version,
    validate_checkout,
)
from tools.verify_boot import GOLDEN
from tools.verify_container import ARCHITECTURES as VERIFIED_ARCHITECTURES
from tools.verify_container import ImageVerifier
from tools.verify_publication_gate import check_release_approval, validate_workflows

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


@pytest.mark.parametrize("outcome", ["no-manifest", "timeout"])
def test_failed_native_container_cases_are_still_recorded(monkeypatch, tmp_path, outcome):
    verifier = ImageVerifier.__new__(ImageVerifier)
    verifier.args = argparse.Namespace(native_source=tmp_path, timeout_scale=2)
    verifier.docker, verifier.cases = ["docker"], []
    (tmp_path / "case/run").mkdir(parents=True)

    @contextlib.contextmanager
    def container(name, command, mounts):
        yield "id", tmp_path / "case"

    def start(command, **kwargs):
        assert kwargs["timeout"] == 2400
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 2)

    verifier.container = container
    monkeypatch.setattr(verify_container.subprocess, "run", start)
    with pytest.raises(RuntimeError, match="native container native-smoke-8 failed"):
        verifier.execute_native("native-smoke-8", "esp32-8-single", "release-smoke-8")
    [record] = verifier.cases
    assert record["case"] == "native-smoke-8" and record["expected_exit"] == 0
    if outcome == "timeout":
        assert record["exit_code"] is None and "exceeded 2400 s" in record["error"]
    else:
        assert record["exit_code"] == 2 and "no run.json" in record["error"]


def candidate(architecture="amd64"):
    image_id = IMAGE_ID if architecture == "amd64" else "sha256:" + "e" * 64
    info = {
        "Id": image_id,
        "Architecture": architecture,
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
        "image": {"Id": image_id, "Architecture": architecture},
        "resources": {"version": "1.2.3", "provenance": {"source_revision": REVISION}},
        "cases": [
            {"case": name, "exit_code": code, "expected_exit": code}
            for name, code in REQUIRED_CASES.items()
        ],
    }
    return proof, info


def platform_digest(tag):
    return "sha256:" + hashlib.sha256(tag.encode()).hexdigest()


def candidates():
    return {architecture: candidate(architecture) for architecture in ARCHITECTURES}


class ControlledRegistry:
    """A fake registry pair: GHCR (this object) plus an optional Docker Hub mirror."""

    def __init__(
        self, *, existing=False, newer=False, mirror_existing=False, mirror_diverges=False
    ):
        self.pushed = {"1.2.3": DIGEST} if existing else {}
        self.commands, self.checks = [], []
        self.newer = newer
        self.mirror = MirrorRegistry({"1.2.3": DIGEST} if mirror_existing else {})
        self.mirror_diverges = mirror_diverges

    def digest(self, tag):
        self.checks.append(tag)
        return self.pushed.get(tag)

    def tags(self):
        return list(self.pushed) + (["2.0.0"] if self.newer else [])

    def docker(self, command):
        self.commands.append(command)
        if command[0] == "push":
            tag = command[1].rsplit(":", 1)[1]
            self.pushed[tag] = platform_digest(tag)
        elif command[:3] == ["buildx", "imagetools", "create"]:
            for target in command[4::2][: command.count("-t")]:
                image, tag = target.rsplit(":", 1)
                if image == IMAGE:
                    self.pushed[tag] = DIGEST
                else:
                    self.mirror.pushed[tag] = (
                        "sha256:" + "f" * 64 if self.mirror_diverges else DIGEST
                    )
        return ""


class MirrorRegistry:
    def __init__(self, pushed):
        self.pushed = pushed

    def digest(self, tag):
        return self.pushed.get(tag)


def test_builtins_resolve_outside_checkout_and_explicit_bad_paths_do_not_fall_back(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    assert len(load_hardware("esp32-8-single")["modules"]) == 8
    assert len(load_hardware("esp32-16-dual.yaml")["modules"]) == 16
    assert len(load_hardware("esp32-8-single-realistic")["modules"]) == 8
    assert len(load_hardware("esp32-16-dual-realistic.yaml")["modules"]) == 16
    assert len(load_hardware("esp32-8-single-perfect")["modules"]) == 8
    assert len(load_hardware("esp32-16-dual-perfect")["modules"]) == 16
    calibration = load_hardware("esp32-8-single-calibration")
    assert (
        load_executor("calibration-8", calibration, variables=["FIRMWARE_VERSION=v1.4.1"]).document[
            "name"
        ]
        == "calibration-8"
    )
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
        promote(
            {**candidates(), "amd64": (proof, info)},
            REVISION,
            "1.2.3",
            True,
            registry,
            registry.docker,
        )
    assert not registry.commands and not registry.checks


@pytest.mark.parametrize("mutation", ["missing", "swapped", "extra"])
def test_a_release_needs_exactly_one_verified_candidate_per_platform(mutation):
    verified = candidates()
    if mutation == "missing":
        del verified["arm64"]
    elif mutation == "swapped":
        verified["arm64"] = verified["amd64"]
    else:
        verified["riscv64"] = candidate("riscv64")
    registry = ControlledRegistry()
    with pytest.raises(ValueError):
        promote(verified, REVISION, "1.2.3", True, registry, registry.docker)
    assert not registry.commands and not registry.checks


@pytest.mark.parametrize("where", ["ghcr", "platform", "mirror"])
def test_existing_immutable_version_refuses_overwrite(where):
    registry = ControlledRegistry(existing=where == "ghcr", mirror_existing=where == "mirror")
    if where == "platform":
        registry.pushed["1.2.3-amd64"] = DIGEST
    with pytest.raises(RuntimeError, match="already exists"):
        promote(
            candidates(),
            REVISION,
            "1.2.3",
            True,
            registry,
            registry.docker,
            mirror=("docker.io/drewferg11/split-flap-emulator", registry.mirror),
        )
    assert not registry.commands


@pytest.mark.parametrize(
    "stable,newer,latest", [(True, False, True), (False, False, False), (True, True, False)]
)
@pytest.mark.parametrize("mirrored", [False, True])
def test_verified_images_are_pushed_and_joined_without_rebuild(stable, newer, latest, mirrored):
    registry = ControlledRegistry(newer=newer)
    hub = "docker.io/drewferg11/split-flap-emulator"
    mirror = (hub, registry.mirror) if mirrored else None
    result = promote(
        candidates(), REVISION, "1.2.3", stable, registry, registry.docker, mirror=mirror
    )
    assert result["reference"] == f"{IMAGE}@{DIGEST}"
    assert result["mirror_reference"] == (f"{hub}@{DIGEST}" if mirrored else None)
    assert result["latest_promoted"] is latest
    assert result["platform_digests"] == {a: platform_digest(f"1.2.3-{a}") for a in ARCHITECTURES}
    tags = [c for c in registry.commands if c[0] == "tag"]
    assert tags == [["tag", candidates()[a][1]["Id"], f"{IMAGE}:1.2.3-{a}"] for a in ARCHITECTURES]
    *_, create = registry.commands
    targets = [f"{IMAGE}:1.2.3"] + ([f"{IMAGE}:latest"] if latest else [])
    if mirrored:
        targets += [f"{hub}:1.2.3"] + ([f"{hub}:latest"] if latest else [])
    assert create == ["buildx", "imagetools", "create"] + [
        x for target in targets for x in ["-t", target]
    ] + [f"{IMAGE}@" + platform_digest(f"1.2.3-{a}") for a in ARCHITECTURES]
    assert len(registry.commands) == 5


def test_mirror_digest_must_match_the_ghcr_index():
    registry = ControlledRegistry(mirror_diverges=True)
    with pytest.raises(RuntimeError, match="does not identify the published index"):
        promote(
            candidates(),
            REVISION,
            "1.2.3",
            True,
            registry,
            registry.docker,
            mirror=("docker.io/drewferg11/split-flap-emulator", registry.mirror),
        )


def test_candidate_artifacts_are_read_with_their_platform(tmp_path):
    proof, info = candidate("arm64")
    (tmp_path / "container").mkdir()
    (tmp_path / "container/verification.json").write_text(json.dumps(proof))
    (tmp_path / "candidate-id.txt").write_text(info["Id"] + "\n")
    calls = []

    def docker(command):
        calls.append(command)
        return json.dumps([info])

    assert load_candidate(tmp_path, docker) == ("arm64", (proof, info))
    assert calls == [["image", "inspect", info["Id"]]]


def test_published_and_verified_platforms_agree():
    assert VERIFIED_ARCHITECTURES == ARCHITECTURES


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


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda jobs: jobs["authorize"].pop("environment"), "authorize must use release"),
        (lambda jobs: jobs["publish"].pop("environment"), "publish must use release"),
        (lambda jobs: jobs["validate"].pop("needs"), "does not wait for authorize"),
        (lambda jobs: jobs["verify"].update(needs=[]), "does not wait for authorize"),
        (lambda jobs: jobs["publish"].pop("needs"), "does not wait for authorize"),
    ],
)
def test_release_jobs_cannot_bypass_owner_approval(mutate, message):
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/release.yml"
    publish = yaml.safe_load(workflow.read_text())
    check_release_approval(publish)
    mutate(publish["jobs"])
    with pytest.raises(AssertionError, match=message):
        check_release_approval(publish)


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
