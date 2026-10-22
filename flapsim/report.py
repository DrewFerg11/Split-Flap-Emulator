"""Finalize Markdown/JUnit evidence and a standalone, safely embedded timeline replay."""

import html
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path


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


def write_replay(out, frames, warning):
    # Encode '<' so firmware/configuration text can never close the script element.
    payload = json.dumps(frames, ensure_ascii=True).replace("<", "\\u003c").replace("&", "\\u0026")
    notice = html.escape(warning or ("No replay snapshots available." if not frames else ""))
    document = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Split-flap replay</title>
<style>
body{font:16px system-ui;background:#15181c;color:#eee;max-width:1000px;
margin:2rem auto;padding:1rem}
.row{display:flex;gap:.35rem;margin:1rem 0;flex-wrap:wrap}.flap{display:grid;place-items:center;
font:42px monospace;background:#292d34;min-width:1.4em;height:1.7em;border-radius:.15em;
border-bottom:3px solid #050607}.moving{outline:2px solid #ffbe58}
.between{border-bottom-color:#ff6658}.fraction{font:12px system-ui;padding:.2rem}
input{width:100%}button{padding:.5rem}
pre{white-space:pre-wrap}#warning{color:#ffbe58}
</style><h1>Split-flap replay</h1><p id="warning">NOTICE</p>
<button id="play" type="button">Play</button>
<label for="scrub">Timeline snapshot</label>
<input id="scrub" type="range" min="0" value="0" step="1">
<div id="board" aria-label="Virtual display"></div><pre id="details"></pre>
<script id="frames" type="application/json">PAYLOAD</script>
<script>
'use strict';
const frames=JSON.parse(document.getElementById('frames').textContent);
const scrub=document.getElementById('scrub'),play=document.getElementById('play');
const board=document.getElementById('board'),details=document.getElementById('details');
let timer=null;
function pause(){clearInterval(timer);timer=null;play.textContent='Play';}
function draw(){
  const frame=frames[Number(scrub.value)];board.replaceChildren();
  if(!frame){details.textContent='No complete timeline snapshots.';return;}
  for(const bus of [0,1]){
    const row=document.createElement('div');row.className='row';
    for(const module of frame.modules.filter(m=>m.bus===bus)){
      const flap=document.createElement('span');
      flap.className='flap'+(module.energized?' moving':'');
      flap.textContent=module.char===' '?'\u00a0':module.char;
      flap.title=`Bus ${module.bus}, address ${module.addr}, angle ${module.angle}`;
      if(Number.isFinite(module.flap_position)&&Number.isFinite(module.flap_error)){
        if(Math.abs(module.flap_error)>0.25)flap.classList.add('between');
        const fraction=document.createElement('small');fraction.className='fraction';
        fraction.textContent=`${module.flap_position.toFixed(3)} (${module.flap_error.toFixed(3)})`;
        flap.append(fraction);flap.title+=`, flap position ${module.flap_position}`;
      }
      row.append(flap);
    }
    board.append(row);
  }
  details.textContent=JSON.stringify({snapshot:Number(scrub.value)+1,total:frames.length,
    guest_seconds:frame.guest_ns/1e9,event:frame.event,movement:frame.movement_id,
    settlement:frame.settlement_id,settled:frame.settled,counters:frame.counters,
    mechanics_revision:frame.mechanics_revision??0,last_operation:frame.last_operation,
    mechanics:frame.modules.map(m=>({bus:m.bus,addr:m.addr,mechanics:m.mechanics,
      faults:m.faults,fault_counters:m.fault_counters}))},null,2);
}
scrub.max=Math.max(0,frames.length-1);scrub.disabled=!frames.length;play.disabled=!frames.length;
scrub.addEventListener('input',()=>{pause();draw();});
play.addEventListener('click',()=>{
  if(timer){pause();return;}if(Number(scrub.value)>=frames.length-1)scrub.value=0;
  play.textContent='Pause';timer=setInterval(()=>{
    if(Number(scrub.value)>=frames.length-1){pause();return;}
    scrub.value=Number(scrub.value)+1;draw();
  },100);draw();
});draw();
</script></html>
""".replace("PAYLOAD", payload).replace("NOTICE", notice, 1)
    (out / "replay.html").write_text(document)


def write_reports(out: Path, manifest, steps, known_issues=None):
    frames, warning = replay_data(out)
    write_replay(out, frames, warning)
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
