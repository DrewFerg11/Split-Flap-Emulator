"""Generate the SFD#86 splice regexes in baselines/v1.4.1-golden.yaml and v1.4.1-native.yaml.

v1.4.1 logs a request body from exactly two handlers in SplitFlapWebServer.cpp, POST /text and
POST /settings: "Received <text|settings> update request", then Serial.println() of the body as
ArduinoJson re-serializes it (compact, keys in the order received). The loop task's fresh-NVS
diagnostic is printed without a shared lock, so the two outputs get spliced (SFD#86). The baseline
entries accept only that request log, or a body (or the start of one) in these shapes:

- POST /text: {"mode":"<a v1.4.1 mode>", then scalar members the handler reads}.
- POST /settings: scalar members named after v1.4.1's settings keys ("mode" is a number here).

Strings may not contain brackets (the diagnostic starts with "[") or an error keyword, and use only
the escapes ArduinoJson 7.4.3 writes. Arrays (e.g. "words") are not accepted, so a body with one is
still reported.

python -m tools.sfd86_patterns prints the four regexes, and --write puts them in both baselines;
tests/test_scenario.py checks both baselines carry exactly these.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINES = (ROOT / "baselines/v1.4.1-golden.yaml", ROOT / "baselines/v1.4.1-native.yaml")
ISSUE = "DrewFerg11/Split-Flap-Display#86"
DIAGNOSTIC_HEAD = "Preferences.cpp:50] begin(): "
DIAGNOSTIC = DIAGNOSTIC_HEAD + "nvs_open failed: NOT_FOUND"
ENDPOINTS = ("text", "settings")
TEXT_MODES = ("single", "multiple", "dual-single", "dual-multiple", "accuracy")
TEXT_KEYS = ("row1", "row2", "center", "delay", "stepSize")
SETTINGS_KEYS = (
    "name", "mdns", "otaPass", "timezone", "dateFormat", "timeFormat", "ssid", "password",
    "mqtt_server", "mqtt_port", "mqtt_user", "mqtt_pass", "wireAddresses", "wireOffsets",
    "sdaPin", "sclPin", "wire1Addresses", "wire1Offsets", "sda2Pin", "scl2Pin",
    "magnetPosition", "displayOffset", "stepsPerRot", "maxVel", "charset",
)  # fmt: skip
SETTINGS_NUMBERS = ("mode",)  # an integer setting, unlike /text's "mode" string
KEYWORDS = r"(?!(?i:error|failed|panic|abort|exception))"
# Digits and padding are ASCII, as the firmware prints them: Python's \d and \s also match
# other scripts' digits and spaces.


class Pattern:
    """A regex for a language and one for every prefix of it (the empty one included)."""

    def __init__(self, full, prefix):
        self.full, self.prefix = full, prefix


def _trie(words):
    root = {}
    for word in words:
        node = root
        for char in word:
            node = node.setdefault(char, {})
    return root


def _prefixes(node):
    if not node:
        return ""
    return "(?:" + "|".join(re.escape(c) + _prefixes(child) for c, child in node.items()) + ")?"


def words(*options):
    full = "|".join(map(re.escape, options))
    return Pattern(full if len(options) == 1 else f"(?:{full})", _prefixes(_trie(options)))


def seq(*parts):
    first, rest = parts[0], parts[1:]
    if not rest:
        return first
    tail = seq(*rest)
    return Pattern(first.full + tail.full, f"(?:{first.full}{tail.prefix}|{first.prefix})")


def alt(*parts):
    return Pattern(
        "(?:" + "|".join(p.full for p in parts) + ")",
        "(?:" + "|".join(p.prefix for p in parts) + ")",
    )


def star(part):
    return Pattern(f"(?:{part.full})*", f"(?:{part.full})*{part.prefix}")


def optional(part):
    return Pattern(f"(?:{part.full})?", part.prefix)


def raw(full, prefix):
    return Pattern(full, prefix)


# A JSON string without brackets or error keywords, escaped as ArduinoJson 7.4.3 writes it
# (TextFormatter::writeChar): the short escapes, \u0000 for NUL, every other byte raw.
ESCAPES = words(*(f"\\{c}" for c in '"\\bfnrt'), "\\u0000")
STRING = seq(words('"'), star(alt(raw(rf'{KEYWORDS}[^"\\\[\]]', ""), ESCAPES)), words('"'))
# TextFormatter::writeFloat: [-]integral[.decimals][e[-]exponent]; NaN and infinities are null.
# writeInteger never pads, so only the decimals may start with zeros, and a written exponent is
# never zero.
INTEGER = raw(r"(?:0|[1-9][0-9]*)", r"(?:0|[1-9][0-9]*)?")
NUMBER = seq(
    optional(words("-")),
    INTEGER,
    optional(seq(words("."), raw(r"[0-9]+", r"[0-9]*"))),
    optional(seq(words("e"), optional(words("-")), raw(r"[1-9][0-9]*", r"(?:[1-9][0-9]*)?"))),
)
SCALAR = alt(STRING, NUMBER, words("true", "false", "null"))


def _members(keys, numbers=()):
    named = seq(words(*keys), words('":'), SCALAR)
    if numbers:
        named = alt(named, seq(words(*numbers), words('":'), NUMBER))
    return seq(words('"'), named)


def _more(member):
    return star(seq(words(","), member))


TEXT_BODY = seq(
    words('{"mode":"'), words(*TEXT_MODES), words('"'), _more(_members(TEXT_KEYS)), words("}")
)
SETTING = _members(SETTINGS_KEYS, SETTINGS_NUMBERS)
SETTINGS_BODY = seq(words("{"), SETTING, _more(SETTING), words("}"))
BODY = alt(TEXT_BODY, SETTINGS_BODY)
REQUEST_LOG = seq(words("Received "), words(*ENDPOINTS), words(" update request"))
ERROR = r"\[ *[0-9]+\]\[E\]"
TIMESTAMP = ERROR + r"\["


def _suffixes(text):
    """Every suffix of text that is not empty, as a regex: (?:(?:a)?b)?c... style."""
    regex = ""
    for char in text[:-1]:
        regex = f"(?:{regex}{re.escape(char)})?"
    return regex + re.escape(text[-1])


def tail():
    # The diagnostic resumes anywhere up to "nvs_open", after the other task's partial line: the
    # start of its request log or of a body. Cuts inside the timestamp keep its digits.
    head = re.escape("][E][" + DIAGNOSTIC_HEAD)
    resume = rf"(?:\[? *[0-9]{{1,10}}{head}|(?:{_suffixes('][E][' + DIAGNOSTIC_HEAD)})?)"
    return (
        rf"^(?!{ERROR})(?:{REQUEST_LOG.prefix}|{BODY.prefix}){resume}"
        + re.escape(DIAGNOSTIC[len(DIAGNOSTIC_HEAD) :])
        + "$"
    )


def head():
    # The diagnostic up to some character, then the rest of the other task's line: its request
    # log or a body (or the start of one).
    return (
        f"^{TIMESTAMP}{words(DIAGNOSTIC).prefix}"
        rf"(?:{REQUEST_LOG.full}|(?=\{{){BODY.prefix})$"
    )


def middle():
    # The other task's request log or whole body, without its newline, anywhere inside the
    # diagnostic after its "[", which then finishes unchanged. The lookahead pins where the
    # insertion sits, with the insertion as an opaque token: the diagnostic has no braces and no
    # "Received", so the token spans exactly the body (first "{" to last "}") or the log. The
    # main pattern then checks that token's shape once, instead of at every split point.
    rest = "][E][" + DIAGNOSTIC
    token = r"(?:Received [a-z]+ update request|\{.*\})"
    splits = [re.escape(rest[:cut]) + token + re.escape(rest[cut:]) for cut in range(len(rest))]
    # Inside the timestamp: among its spaces, or among its digits.
    stamp = rf"(?: *{token} *[0-9]+| *[0-9]+{token}[0-9]*){re.escape(rest)}"
    where = rf"\[(?:{stamp}| *[0-9]+(?:" + "|".join(splits) + "))"
    return f"^(?={where}$)(?:[^{{}}]*{BODY.full}[^{{}}]*|[^{{}}]*{REQUEST_LOG.full}[^{{}}]*)$"


def request_body():
    # A whole body, then the diagnostic's head up to "begin(): " (the last cut tail() resumes).
    return f"^{BODY.full}{TIMESTAMP}{words(DIAGNOSTIC_HEAD).prefix}$"


def patterns():
    """The four SFD#86 regexes, in baseline order."""
    return [tail(), head(), middle(), request_body()]


def write(paths=BASELINES):
    """Replace the #86 regexes in each baseline, in order, leaving every other line as it is."""
    for path in paths:
        lines, regexes, issue = [], iter(patterns()), None
        for line in path.read_text().splitlines(keepends=True):
            if line.startswith("  - issue: "):
                issue = line.split(":", 1)[1].strip()
            if issue == ISSUE and line.startswith("    regex: "):
                line = f"    regex: '{next(regexes)}'\n"
            lines.append(line)
        if next(regexes, None) is not None:
            raise ValueError(f"{path}: expected {len(patterns())} {ISSUE} regexes")
        path.write_text("".join(lines))


if __name__ == "__main__":
    if sys.argv[1:] == ["--write"]:
        write()
    else:
        print("\n".join(patterns()))
