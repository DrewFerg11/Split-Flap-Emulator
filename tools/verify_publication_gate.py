"""Exercise real candidate rejection and the publication workflow's fail-closed dependency."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

from tools.publish_image import promote
from tools.verify_boot import GOLDEN, ROOT


def validate_workflows(root):
    publish = yaml.safe_load((root / ".github/workflows/publish.yml").read_text())
    jobs = publish["jobs"]
    assert set(jobs["publish"]["needs"]) >= {"verify", "validate"}
    condition = jobs["publish"]["if"]
    assert "github.event_name == 'push'" in condition and "refs/tags/v" in condition
    assert "DrewFerg11/Split-Flap-Emulator" in condition
    assert publish["permissions"] == {"contents": "read"}
    for name, job in jobs.items():
        if name != "publish":
            assert job.get("permissions", {}).get("packages") != "write"
    assert jobs["publish"]["permissions"]["packages"] == "write"
    verify = yaml.safe_load((root / ".github/workflows/verify-container.yml").read_text())
    assert verify["permissions"] == {"contents": "read"}
    steps = verify["jobs"]["image"]["steps"]
    verify_index = next(
        i for i, s in enumerate(steps) if "tools.verify_container" in s.get("run", "")
    )
    export_index = next(i for i, s in enumerate(steps) if "docker save" in s.get("run", ""))
    assert verify_index < export_index
    assert "success()" in steps[export_index]["if"]
    assert not any(s.get("with", {}).get("push") for s in steps)


class ForbiddenPublication:
    def digest(self, *args):
        raise AssertionError("rejected candidate reached registry publication")

    def __call__(self, *args):
        raise AssertionError("rejected candidate attempted Docker mutation")


def verify(args):
    validate_workflows(ROOT)
    if args.out.exists() and any(args.out.iterdir()):
        raise ValueError("publication-gate evidence must be fresh")
    args.out.mkdir(parents=True, exist_ok=True)
    rejected = args.out / "rejected"
    command = [
        sys.executable,
        "-m",
        "tools.verify_container",
        "--image",
        args.image,
        "--mode",
        "reject",
        "--out",
        str(rejected),
        "--firmware",
        str(args.firmware),
    ]
    if args.docker_host:
        command += ["--docker-host", args.docker_host]
    if args.expected_source:
        command += ["--expected-source", args.expected_source]
    completed = subprocess.run(command, check=False)
    proof = json.loads((rejected / "verification.json").read_text())
    if completed.returncode != 1 or proof["passed"] is not False:
        raise RuntimeError("deliberately wrong version unexpectedly passed candidate verification")
    if proof["cases"][0]["exit_code"] != 5:
        raise RuntimeError("candidate was not rejected for its version assertion")
    info = {**proof["image"], "Config": {"Labels": proof["image"]["labels"]}}
    forbidden = ForbiddenPublication()
    try:
        promote(
            proof, info, args.expected_source or "unrecorded", "9.9.9", False, forbidden, forbidden
        )
    except ValueError:
        pass
    else:
        raise RuntimeError("rejected candidate passed publication validation")
    result = {
        "passed": True,
        "candidate_rejected": True,
        "cli_exit_code": 5,
        "publication_attempts": 0,
        "workflow_dependency_verified": True,
    }
    (args.out / "verification.json").write_text(json.dumps(result, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--firmware", type=Path, default=ROOT / "firmware" / GOLDEN["asset"])
    parser.add_argument("--docker-host")
    parser.add_argument("--expected-source")
    args = parser.parse_args()
    try:
        verify(args)
    except Exception as error:
        print(f"Publication-gate verification failed: {error}", file=sys.stderr)
        return 1
    print("PASS: rejected candidate cannot reach publication; workflow dependency checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
