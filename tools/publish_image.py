"""Promote verified per-architecture candidates to GHCR as one multi-arch image, without
rebuilding or overwriting a version, and optionally mirror it to Docker Hub (#23)."""

import argparse
import base64
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from packaging.version import Version
from setuptools_scm import get_version

REPOSITORY = "drewferg11/split-flap-emulator"
IMAGE = "ghcr.io/" + REPOSITORY
# Every release carries exactly these platforms, each verified on its own native runner.
ARCHITECTURES = ("amd64", "arm64")
MIRROR_PATTERN = r"docker\.io/[a-z0-9]+(?:[._-][a-z0-9]+)*/[a-z0-9]+(?:[._-][a-z0-9]+)*"
MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
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
    # The version is derived from the git tag (setuptools-scm); a tag that did not resolve to
    # itself, such as a checkout without the tag, yields a dev/local version and is rejected below.
    package_version = get_version(root=".")
    return release_version(tag, package_version) if tag else (package_version, False)


def validate_candidate(proof, info, revision, version, architecture):
    if (
        proof.get("passed") is not True
        or proof.get("mode") != "all"
        or proof.get("timeout_scale") != 1
        or proof.get("image", {}).get("Id") != info.get("Id")
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(info.get("Id", "")))
        or info.get("Architecture") != architecture
        or proof.get("image", {}).get("Architecture") != architecture
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
    """Read-only manifest/tag queries against GHCR or Docker Hub's registry API."""

    def __init__(
        self,
        actor,
        token,
        *,
        base_url="https://ghcr.io",
        token_url=None,
        service="ghcr.io",
        repository=REPOSITORY,
        name="GHCR",
    ):
        self.base_url, self.repository, self.name = base_url, repository, name
        self.opener = urllib.request.build_opener(NoRedirect)
        query = urllib.parse.urlencode(
            {"service": service, "scope": f"repository:{repository}:pull,push"}
        )
        credentials = base64.b64encode(f"{actor}:{token}".encode()).decode()
        request = urllib.request.Request(
            (token_url or base_url + "/token") + "?" + query,
            headers={"Authorization": "Basic " + credentials},
        )
        with self.opener.open(request, timeout=30) as response:
            self.token = json.load(response)["token"]
        if not self.token:
            raise RuntimeError(f"{name} did not grant a registry token")

    @classmethod
    def docker_hub(cls, actor, token, image):
        return cls(
            actor,
            token,
            base_url="https://registry-1.docker.io",
            token_url="https://auth.docker.io/token",
            service="registry.docker.io",
            repository=image.removeprefix("docker.io/"),
            name="Docker Hub",
        )

    def digest(self, tag):
        request = urllib.request.Request(
            f"{self.base_url}/v2/{self.repository}/manifests/" + urllib.parse.quote(tag, safe=""),
            method="HEAD",
            headers={"Authorization": "Bearer " + self.token, "Accept": MANIFEST_TYPES},
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                digest = response.headers.get("Docker-Content-Digest", "")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise RuntimeError(
                f"{self.name} manifest check failed with HTTP {error.code}"
            ) from error
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise RuntimeError(f"{self.name} returned no valid immutable manifest digest")
        return digest

    def tags(self):
        result = []
        url = f"{self.base_url}/v2/{self.repository}/tags/list?n=100"
        for _ in range(100):
            request = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.token})
            with self.opener.open(request, timeout=30) as response:
                value = json.load(response)
                link = response.headers.get("Link")
            tags = value.get("tags")
            if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
                raise RuntimeError(f"{self.name} returned an invalid tag list")
            result.extend(tags)
            if not link:
                return result
            match = re.fullmatch(r'<([^>]+)>; rel="next"', link)
            if not match:
                raise RuntimeError(f"unsupported {self.name} tag pagination")
            url = urllib.parse.urljoin(url, match[1])
            if not url.startswith(f"{self.base_url}/v2/{self.repository}/tags/list?"):
                raise RuntimeError(f"{self.name} tag pagination left the expected repository")
        raise RuntimeError(f"{self.name} tag pagination exceeded its bound")


def promote(candidates, revision, version, stable, registry, docker, mirror=None):
    """Publish {architecture: (proof, info)} as one index tagged `version` (and `latest` for
    the newest stable release). Each platform is pushed as `version-<arch>` first, so the index
    only references images that passed verification. `mirror` is (image, registry) for Docker
    Hub: the same index is tagged there in the same command, so the digests are identical."""
    for architecture, (proof, info) in candidates.items():
        validate_candidate(proof, info, revision, version, architecture)
    if sorted(candidates) != sorted(ARCHITECTURES):
        raise ValueError(f"a release needs verified candidates for exactly {list(ARCHITECTURES)}")
    targets = [(IMAGE, registry)] + ([mirror] if mirror else [])
    for image, target in targets:
        if target.digest(version) is not None:
            raise RuntimeError(
                f"immutable version {version} already exists in {image}; choose a new release tag"
            )
    # Check every platform tag before pushing any, so a refusal never leaves a partial release.
    for architecture in ARCHITECTURES:
        if registry.digest(f"{version}-{architecture}") is not None:
            raise RuntimeError(f"immutable platform image {version}-{architecture} already exists")
    platforms = {}
    for architecture in ARCHITECTURES:
        tag = f"{version}-{architecture}"
        docker(["tag", candidates[architecture][1]["Id"], f"{IMAGE}:{tag}"])
        docker(["push", f"{IMAGE}:{tag}"])
        platforms[architecture] = registry.digest(tag)
        if platforms[architecture] is None:
            raise RuntimeError(f"pushed platform image {tag} has no readable immutable digest")
    newer_stable = []
    if stable:
        newer_stable = [
            tag
            for tag in registry.tags()
            if re.fullmatch(r"\d+\.\d+\.\d+", tag) and Version(tag) > Version(version)
        ]
    promote_latest = stable and not newer_stable
    tags = [version] + (["latest"] if promote_latest else [])
    command = ["buildx", "imagetools", "create"]
    for image, _ in targets:
        for tag in tags:
            command += ["-t", f"{image}:{tag}"]
    docker(command + [f"{IMAGE}@{platforms[a]}" for a in ARCHITECTURES])
    digest = registry.digest(version)
    if digest is None:
        raise RuntimeError("pushed version has no readable immutable digest")
    for image, target in targets:
        for tag in tags:
            if target.digest(tag) != digest:
                raise RuntimeError(f"{image}:{tag} does not identify the published index {digest}")
    return {
        "passed": True,
        "image_ids": {a: candidates[a][1]["Id"] for a in ARCHITECTURES},
        "platform_digests": platforms,
        "source_revision": revision,
        "version": version,
        "digest": digest,
        "reference": IMAGE + "@" + digest,
        "version_reference": f"{IMAGE}:{version}",
        "mirror_reference": f"{mirror[0]}@{digest}" if mirror else None,
        "latest_promoted": promote_latest,
        "newer_stable_versions": newer_stable,
    }


def load_candidate(directory, docker):
    """A verified-container-candidate artifact: the image ID and its verification proof."""
    proof = json.loads((directory / "container/verification.json").read_text())
    image = (directory / "candidate-id.txt").read_text().strip()
    info = json.loads(docker(["image", "inspect", image]))[0]
    return info.get("Architecture"), (proof, info)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate",
        type=Path,
        action="append",
        default=[],
        help="a downloaded verified-container-candidate artifact (one per architecture)",
    )
    parser.add_argument("--mirror", help="also tag the index here, e.g. docker.io/owner/name")
    parser.add_argument("--revision", required=True)
    parser.add_argument("--tag")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if not args.validate_only and (not args.candidate or not args.tag):
        parser.error("--candidate and --tag are required for publication")
    if args.mirror and not re.fullmatch(MIRROR_PATTERN, args.mirror):
        parser.error("--mirror must be docker.io/<namespace>/<name> in lowercase")
    result = {"passed": False}

    def docker(command):
        return subprocess.run(
            ["docker"] + command, check=True, text=True, stdout=subprocess.PIPE
        ).stdout

    try:
        version, stable = validate_checkout(args.revision, args.tag)
        if args.validate_only:
            result = {"passed": True, "version": version, "stable": stable}
        else:
            candidates = dict(load_candidate(path, docker) for path in args.candidate)
            if len(candidates) != len(args.candidate):
                raise ValueError("each candidate must be a different architecture")
            registry = Registry(os.environ["GITHUB_ACTOR"], os.environ["GH_TOKEN"])
            mirror = None
            if args.mirror:
                hub = Registry.docker_hub(
                    os.environ["DOCKERHUB_USERNAME"], os.environ["DOCKERHUB_TOKEN"], args.mirror
                )
                mirror = (args.mirror, hub)
            result = promote(
                candidates, args.revision, version, stable, registry, docker, mirror=mirror
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
