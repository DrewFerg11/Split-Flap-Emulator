"""Promote a verified local candidate to GHCR without rebuilding or overwriting a version."""

import argparse
import base64
import json
import os
import re
import subprocess
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from packaging.version import Version

IMAGE = "ghcr.io/drewferg11/flapsim"
REQUIRED_CASES = {"smoke-8": 0, "smoke-16": 0, "timeout": 4, "stop": 143, "wrong-version": 5}


def release_version(tag, package_version):
    if not re.fullmatch(
        r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?",
        tag,
    ):
        raise ValueError(
            "release tag must be vMAJOR.MINOR.PATCH with an optional prerelease suffix"
        )
    if Version(tag[1:]) != Version(package_version):
        raise ValueError(f"release tag {tag} does not match package version {package_version}")
    return tag[1:], bool(re.fullmatch(r"v\d+\.\d+\.\d+", tag))


def validate_checkout(revision, tag=None):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("source revision must be a full commit SHA")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if head != revision:
        raise ValueError("publication checkout differs from verified source revision")
    subprocess.run(["git", "merge-base", "--is-ancestor", revision, "origin/main"], check=True)
    if subprocess.check_output(["git", "status", "--porcelain"], text=True).strip():
        raise ValueError("publication checkout has uncommitted source changes")
    project = tomllib.loads(Path("pyproject.toml").read_text())
    package_version = project["project"]["version"]
    return release_version(tag, package_version) if tag else (package_version, False)


def validate_candidate(proof, info, revision, version):
    if (
        proof.get("passed") is not True
        or proof.get("mode") != "all"
        or proof.get("timeout_scale") != 1
        or proof.get("image", {}).get("Id") != info.get("Id")
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(info.get("Id", "")))
        or info.get("Architecture") != "amd64"
        or info.get("Os") != "linux"
    ):
        raise ValueError("candidate image does not have complete matching image-level verification")
    resources = proof.get("resources", {})
    if (
        resources.get("provenance", {}).get("source_revision") != revision
        or info.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision")
        != revision
        or Version(resources.get("version", "0")) != Version(version)
        or Version(
            info.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.version", "0")
        )
        != Version(version)
    ):
        raise ValueError("candidate version/source provenance does not match the release")
    cases = proof.get("cases", [])
    if (
        len(cases) != len(REQUIRED_CASES)
        or {c.get("case"): c.get("exit_code") for c in cases} != REQUIRED_CASES
        or any(c.get("expected_exit") != REQUIRED_CASES[c["case"]] for c in cases)
    ):
        raise ValueError("candidate is missing required positive/negative container checks")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Registry:
    def __init__(self, actor, token, *, base_url="https://ghcr.io"):
        self.base_url = base_url
        self.opener = urllib.request.build_opener(NoRedirect)
        query = urllib.parse.urlencode(
            {"service": "ghcr.io", "scope": "repository:drewferg11/flapsim:pull,push"}
        )
        credentials = base64.b64encode(f"{actor}:{token}".encode()).decode()
        request = urllib.request.Request(
            base_url + "/token?" + query, headers={"Authorization": "Basic " + credentials}
        )
        with self.opener.open(request, timeout=30) as response:
            self.token = json.load(response)["token"]
        if not self.token:
            raise RuntimeError("GHCR did not grant a registry token")

    def digest(self, tag):
        request = urllib.request.Request(
            self.base_url + "/v2/drewferg11/flapsim/manifests/" + urllib.parse.quote(tag, safe=""),
            method="HEAD",
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.oci.image.manifest.v1+json, "
                "application/vnd.docker.distribution.manifest.v2+json",
            },
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                digest = response.headers.get("Docker-Content-Digest", "")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise RuntimeError(f"GHCR manifest check failed with HTTP {error.code}") from error
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise RuntimeError("GHCR returned no valid immutable manifest digest")
        return digest

    def tags(self):
        result = []
        url = self.base_url + "/v2/drewferg11/flapsim/tags/list?n=100"
        for _ in range(100):
            request = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.token})
            with self.opener.open(request, timeout=30) as response:
                value = json.load(response)
                link = response.headers.get("Link")
            tags = value.get("tags")
            if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
                raise RuntimeError("GHCR returned an invalid tag list")
            result.extend(tags)
            if not link:
                return result
            match = re.fullmatch(r'<([^>]+)>; rel="next"', link)
            if not match:
                raise RuntimeError("unsupported GHCR tag pagination")
            url = urllib.parse.urljoin(url, match[1])
            if not url.startswith(self.base_url + "/v2/drewferg11/flapsim/tags/list?"):
                raise RuntimeError("GHCR tag pagination left the expected repository")
        raise RuntimeError("GHCR tag pagination exceeded its bound")


def promote(proof, info, revision, version, stable, registry, docker):
    validate_candidate(proof, info, revision, version)
    if registry.digest(version) is not None:
        raise RuntimeError(f"immutable version {version} already exists; choose a new release tag")
    reference = f"{IMAGE}:{version}"
    docker(["tag", info["Id"], reference])
    docker(["push", reference])
    digest = registry.digest(version)
    if digest is None:
        raise RuntimeError("pushed version has no readable immutable digest")
    newer_stable = []
    if stable:
        newer_stable = [
            tag
            for tag in registry.tags()
            if re.fullmatch(r"\d+\.\d+\.\d+", tag) and Version(tag) > Version(version)
        ]
    promote_latest = stable and not newer_stable
    if promote_latest:
        docker(["tag", info["Id"], IMAGE + ":latest"])
        docker(["push", IMAGE + ":latest"])
        if registry.digest("latest") != digest:
            raise RuntimeError("latest does not identify the verified version's manifest")
    return {
        "passed": True,
        "image_id": info["Id"],
        "source_revision": revision,
        "version": version,
        "digest": digest,
        "reference": IMAGE + "@" + digest,
        "version_reference": reference,
        "latest_promoted": promote_latest,
        "newer_stable_versions": newer_stable,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof", type=Path)
    parser.add_argument("--image")
    parser.add_argument("--revision", required=True)
    parser.add_argument("--tag")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if not args.validate_only and (not args.proof or not args.image or not args.tag):
        parser.error("--proof, --image and --tag are required for publication")
    result = {"passed": False}
    try:
        version, stable = validate_checkout(args.revision, args.tag)
        if args.validate_only:
            result = {"passed": True, "version": version, "stable": stable}
        else:
            proof = json.loads(args.proof.read_text())
            info = json.loads(
                subprocess.check_output(["docker", "image", "inspect", args.image], text=True)
            )[0]
            validate_candidate(proof, info, args.revision, version)
            actor, token = os.environ["GITHUB_ACTOR"], os.environ["GH_TOKEN"]
            registry = Registry(actor, token)
            result = promote(
                proof,
                info,
                args.revision,
                version,
                stable,
                registry,
                lambda command: subprocess.run(["docker"] + command, check=True),
            )
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
