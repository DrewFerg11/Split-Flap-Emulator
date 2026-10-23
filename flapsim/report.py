"""Finalize Markdown/JUnit evidence and a standalone, safely embedded timeline replay."""

import html
import json
import os
import re
import xml.etree.ElementTree as ET
from importlib.resources import files
from pathlib import Path

from flapsim.ui import assemble


def fenced(value):
    text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=True)
    fence = "`" * max(3, max((len(part) for part in re.findall(r"`+", text)), default=0) + 1)
    return f"{fence}\n{text}\n{fence}"


def replay_data(out):
    frames = []
    warning = None
    try:
        with (out / "timeline.jsonl").open("rb") as timeline:
            for line in timeline:
                if not line.endswith(b"\n"):
                    warning = "Incomplete trailing timeline record excluded."
                    break
                try:
                    value = json.loads(line)
                    if (
                        not isinstance(value, dict)
                        or not isinstance(value.get("modules"), list)
                        or type(value.get("guest_ns")) is not int
                        or any(
                            not isinstance(m, dict) or not isinstance(m.get("char"), str)
                            for m in value["modules"]
                        )
                    ):
                        raise ValueError("missing snapshot fields")
                    frames.append(value)
                except (ValueError, UnicodeError) as error:
                    warning = f"Replay stops at an invalid timeline record: {error}"
                    break
    except OSError as error:
        warning = f"Timeline unavailable: {error}"
    return frames, warning


MECHANICS_FIELDS = (
    "steps_per_rotation",
    "magnet_position",
    "flap_offset",
    "sensor_window",
    "sensor_polarity",
)


def initial_mechanics(hardware):
    """Each module's configured mechanics by "bus:addr", for the replay's drum dials.

    Timeline snapshots only carry a module's mechanics once they change, so the dials fall
    back to the run's hardware description until then.
    """
    result = {}
    modules = hardware.get("modules") if isinstance(hardware, dict) else None
    for module in modules if isinstance(modules, list) else []:
        if isinstance(module, dict) and type(module.get("bus")) is int:
            if type(module.get("addr")) is int:
                result[f"{module['bus']}:{module['addr']}"] = {
                    k: module[k] for k in MECHANICS_FIELDS if k in module
                }
    return result


def write_replay(out, frames, warning, hardware=None):
    # Encode '<' so firmware/configuration text can never close the script element.
    def embed(value):
        text = json.dumps(value, ensure_ascii=True)
        return text.replace("<", "\\u003c").replace("&", "\\u0026")

    payload = embed(frames)
    notice = html.escape(warning or ("No replay snapshots available." if not frames else ""))
    template = files("flapsim").joinpath("data/replay/replay.html").read_text(encoding="utf-8")
    values = {"NOTICE": notice, "FRAMES": payload, "MECHANICS": embed(initial_mechanics(hardware))}
    # One pass, so text inside the notice or frames is never mistaken for a marker.
    document = re.sub(
        r"@@(NOTICE|FRAMES|MECHANICS)@@", lambda m: values[m.group(1)], assemble(template)
    )
    (out / "replay.html").write_text(document)


def write_reports(out: Path, manifest, steps, known_issues=None):
    frames, warning = replay_data(out)
    write_replay(out, frames, warning, manifest.get("hardware"))
    try:
        serial = (out / "serial.log").read_text(errors="replace").splitlines()[-50:]
    except OSError:
        serial = ["Serial output unavailable."]
    actual = frames[-1] if frames else {}
    manifest.update(
        steps=steps,
        known_issues=known_issues,
        replay={"snapshots": len(frames), "warning": warning},
    )
    applied = bool(known_issues and known_issues["applied"])
    verdict = "PASS" if manifest["passed"] else "FAIL"
    if manifest["passed"] and applied:
        verdict = "PASSED WITH KNOWN ISSUES"
    lines = [
        f"# Firmware scenario: {verdict}",
        "",
        f"Exit code: {manifest['exit_code']}; wall seconds: {manifest['wall_seconds']}",
        "Last observed guest seconds: "
        f"{actual.get('guest_ns', 0) / 1e9 if frames else 'unavailable'}",
        "",
    ]
    if known_issues:
        lines += [f"**KNOWN-ISSUE BASELINE IN USE:** `{known_issues['baseline']}`", ""]
        if known_issues["expected_failure"]:
            failure = known_issues["expected_failure"]
            lines += [
                f"- Expected failure at step {failure['step']} ({failure['action']}): "
                f"{failure['issue']}. {failure['reason']}",
                fenced(failure["error"]),
            ]
        for record in known_issues["tolerated_serial"]:
            lines.append(
                f"- Tolerated {record['count']} serial line(s) for {record['issue']}, "
                f"first: `{record['first']}`"
            )
        if stalls := known_issues.get("tolerated_stalls"):
            lines.append(
                f"- Tolerated {stalls['stalls']} drum stall(s), explained by "
                f"{stalls['allowance']} logged timeout(s): {', '.join(stalls['issues'])}"
            )
        for entry in known_issues["expected_failures_not_observed"]:
            lines.append(
                f"- Not observed this run: {entry['issue']} at step {entry['step']} "
                f"({entry['action']})"
            )
        if not applied:
            lines.append("- No known issue was observed in this run.")
        lines.append("")
    if manifest.get("error"):
        lines += ["Run failure:", fenced(manifest["error"]), ""]
    # Injected faults (#19): the newest model output wins, so the counts are final.
    try:
        faults = json.loads((out / "activity.json").read_text()).get("faults")
    except (OSError, ValueError, AttributeError):
        faults = actual.get("faults")
    if isinstance(faults, list) and faults:
        lines.append("Injected faults:")
        for fault in faults:
            kinds = ", ".join(f"{k} {fault[k]}" for k in ["sensor", "motor", "i2c"] if k in fault)
            lines.append(
                f"- bus {fault.get('bus')} 0x{fault.get('addr', 0):02x}: {kinds}; "
                f"acted {fault.get('events')} time(s)"
            )
        lines.append("")
    lines += [
        "Input provenance:",
        fenced(
            {
                k: manifest.get(k)
                for k in [
                    "mode",
                    "firmware",
                    "native",
                    "emulator",
                    "seed",
                    "hardware",
                    "build",
                    "timing",
                    "scenario",
                    "source_unchanged",
                ]
            }
        ),
        "",
    ]
    for step in steps:
        lines += [
            f"## Step {step['index']}: {step['name']} — {step['status']}",
            f"Wall seconds: {step.get('wall_seconds', 0)}; guest ns: "
            f"{step.get('guest_start_ns')} → {step.get('guest_end_ns')}",
            "Expected:",
            fenced(step.get("expected")),
            "Actual:",
            fenced(step.get("actual")),
        ]
        if step.get("error"):
            lines += ["Failure:", fenced(step["error"])]
        if "flap_errors" in step:
            lines += ["Signed landing errors (flap pitches):", fenced(step["flap_errors"])]
        lines.append("")
    lines += [
        "## Final observed model counters",
        fenced(actual.get("counters", "unavailable")),
        "",
        f"Replay: {len(frames)} complete snapshots. {warning or ''}",
        "",
        "## Last 50 serial lines",
        fenced("\n".join(serial)),
        "",
    ]
    report = "\n".join(lines)
    (out / "report.md").write_text(report)
    suite = ET.Element(
        "testsuite",
        name=manifest.get("scenario", {}).get("name", "firmware-run"),
        time=str(manifest["wall_seconds"]),
    )
    for step in steps:
        case = ET.SubElement(
            suite,
            "testcase",
            name=f"{step['index']}: {step['name']}",
            time=str(step.get("wall_seconds", 0)),
        )
        if step["status"] == "failed":
            failure = ET.SubElement(case, "failure", message=step.get("error", "step failed"))
            failure.text = json.dumps(
                {k: step.get(k) for k in ["expected", "actual", "error", "flap_errors"]}
            )
        elif step["status"] == "xfail":
            ET.SubElement(
                case,
                "skipped",
                message=f"expected failure (known issue {step.get('known_issue')}): "
                f"{step.get('error', '')}",
            )
        elif step["status"] == "skipped_known_issue":
            ET.SubElement(case, "skipped", message="not run after an expected known-issue failure")
        elif step["status"] != "passed":
            ET.SubElement(case, "skipped", message="not run because execution stopped earlier")
    if not steps or (not manifest["passed"] and not any(s["status"] == "failed" for s in steps)):
        case = ET.SubElement(suite, "testcase", name="run lifecycle")
        if not manifest["passed"]:
            failure = ET.SubElement(case, "failure", message=manifest.get("error", "run failed"))
            failure.text = "\n".join(serial)
    suite.set("tests", str(len(suite.findall("testcase"))))
    suite.set("failures", str(len(suite.findall("testcase/failure"))))
    suite.set("skipped", str(len(suite.findall("testcase/skipped"))))
    for element in suite.iter():
        element.attrib = {key: xml_safe(value) for key, value in element.attrib.items()}
        if element.text:
            element.text = xml_safe(element.text)
    ET.ElementTree(suite).write(out / "junit.xml", encoding="utf-8", xml_declaration=True)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as destination:
            destination.write(report)


def xml_safe(value):
    return re.sub(
        r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]", "\ufffd", str(value)
    )
