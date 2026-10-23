"""Native-mode build inputs: Arduino sketch preprocessing, PlatformIO flags, versions, assets."""

import gzip
import json
import subprocess

import pytest

from native import build
from native.sketch import function_definitions, platformio_env, preprocess_sketch
from tools.verify_native import KnownSerial, improv_packet, records

SKETCH = """// leading comment with braces { }
#include "Thing.h"
#define BRACE {
static const char *text = "void fake() { }";
char quote = '{';
JsonSettings settings = JsonSettings("config", {
    {"name", JsonSetting("x")},
});
struct Token
{
    const char *token;
};
/* block void commented() { } */
bool callback(const char *ssid, const char *password) {
    if (ssid) { return true; }
    return false;
}

const char *reason(esp_reset_reason_t value) {
    switch (value) { default: return "x"; }
}
#ifdef DUAL
void dual();
void dualMode() {}
#endif
String later(const String &format) { return format; }
static void helper(int x = 3) {}
"""


def test_top_level_function_prototypes_skip_initializers_types_and_literals():
    found = [prototype for _, prototype in function_definitions(SKETCH)]
    assert found == [
        "bool callback(const char *ssid, const char *password);",
        "const char *reason(esp_reset_reason_t value);",
        "void dualMode();",
        "String later(const String &format);",
    ]


def test_preprocessed_sketch_keeps_text_and_maps_lines():
    result = preprocess_sketch(SKETCH, "/src/Sketch.ino")
    assert result.startswith('#include <Arduino.h>\n#line 1 "/src/Sketch.ino"\n')
    prototypes = "".join(p + "\n" for _, p in function_definitions(SKETCH))
    inserted = prototypes + '#line 14 "/src/Sketch.ino"\n'
    body = result.split("\n", 2)[2]
    assert inserted in body and body.replace(inserted, "", 1) == SKETCH
    lines = result.splitlines()
    marker = next(
        i for i, line in enumerate(lines) if line.startswith('#line 14 "/src/Sketch.ino"')
    )
    assert lines[marker + 1] == "bool callback(const char *ssid, const char *password) {"
    assert lines[marker - 1] == "String later(const String &format);"


def test_unbalanced_sketch_is_rejected():
    with pytest.raises(ValueError, match="unbalanced"):
        function_definitions("void f() {\n")


PLATFORMIO = """
[env]
framework=arduino
build_flags=
    '-D STARTUP_DELAY=2000'
    ; '-D WIFI_SSID="MySsid"' ; commented out
    -DPLAIN

[env:base]
extends=env
board=esp32dev
build_flags=
    ${env.build_flags}
    '-D SDA_PIN=21'
    '-D WIFI_SSID="Home Net"'

[env:child]
extends=env:base

[env:bad]
board=esp32dev
build_flags=-Os
"""


def test_platformio_environment_flags_follow_extends_and_interpolation(tmp_path):
    ini = tmp_path / "platformio.ini"
    ini.write_text(PLATFORMIO)
    assert platformio_env(ini, "child") == {
        "env": "child",
        "board": "esp32dev",
        "defines": {
            "STARTUP_DELAY": "2000",
            "PLAIN": "1",
            "SDA_PIN": "21",
            "WIFI_SSID": '"Home Net"',
        },
    }
    with pytest.raises(ValueError, match="unsupported build flag"):
        platformio_env(ini, "bad")
    with pytest.raises(ValueError, match="no \\[env:missing\\]"):
        platformio_env(ini, "missing")


def git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def test_firmware_version_follows_the_firmware_inject_rule(tmp_path):
    git(tmp_path, "init", "-q")
    git(
        tmp_path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    assert len(build.firmware_version(tmp_path)) == 7  # untagged: abbreviated commit
    git(tmp_path, "tag", "v1.4.1-rc.1")
    git(tmp_path, "tag", "v1.4.1")
    assert build.firmware_version(tmp_path) == "v1.4.1"  # stable outranks its prerelease
    (tmp_path / "tracked").write_text("a")
    git(tmp_path, "add", "tracked")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "y")
    git(tmp_path, "tag", "v1.5.0-beta.2")
    (tmp_path / "tracked").write_text("b")
    assert build.firmware_version(tmp_path) == "v1.5.0-beta.2-dirty"
    assert build.firmware_version(tmp_path / "missing") == "unknown"


def test_local_checkout_must_look_like_the_firmware(tmp_path):
    with pytest.raises(ValueError, match="not a Split-Flap Display checkout"):
        build.resolve_firmware(str(tmp_path), tmp_path / "cache")
    with pytest.raises(ValueError, match="invalid firmware git ref"):
        build.resolve_firmware("git:../escape", tmp_path / "cache")


def test_data_only_littlefs_matches_the_firmware_gzip_layout(tmp_path):
    web = tmp_path / "src/web"
    web.mkdir(parents=True)
    (web / "timezones.json").write_text('{"UTC": "UTC0"}')
    (web / "index.html").write_text("<html></html>")
    out = tmp_path / "out"
    assert build.littlefs(tmp_path, out, "data") == ["timezones.json", "timezones.json.gz"]
    packed = (out / "littlefs/timezones.json.gz").read_bytes()
    assert gzip.decompress(packed) == b'{"UTC": "UTC0"}'
    assert packed[4:8] == b"\0\0\0\0"  # reproducible: no timestamp
    assert build.littlefs(tmp_path, out, "data") == ["timezones.json", "timezones.json.gz"]
    assert (out / "littlefs/timezones.json.gz").read_bytes() == packed


def test_pinned_libraries_are_full_commits():
    manifest = json.loads((build.ROOT / "native/libraries.json").read_text())
    assert {lib["name"] for lib in manifest["libraries"]} == {
        "ArduinoJson",
        "PubSubClient3",
        "Improv WiFi Library",
    }
    for library in manifest["libraries"]:
        assert len(library["commit"]) == 40 and library["repository"].startswith(
            "https://github.com/"
        )


def test_shim_digest_tracks_native_and_model_sources():
    digest = build.shim_digest()
    assert len(digest) == 64 and digest == build.shim_digest()


def test_improv_wifi_settings_frame():
    frame = improv_packet("ab", "c")
    assert frame[:6] == b"IMPROV"
    assert frame[6:9] == bytes([1, 3, 7])  # version, RPC, data length
    assert frame[9:-1] == bytes([1, 5, 2]) + b"ab" + bytes([1]) + b"c"
    assert frame[-1] == sum(frame[:-1]) & 0xFF


def test_native_verifier_tolerates_only_the_linked_preferences_noise():
    known = KnownSerial()
    noise = "[  2020][E][Preferences.cpp:483] getString(): nvs_get_str len fail: name NOT_FOUND"
    assert known.tolerated_serial(noise)["issue"] == "DrewFerg11/Split-Flap-Display#54"
    assert known.tolerated_serial(noise.replace("NOT_FOUND", "INVALID_HANDLE")) is None
    assert known.tolerated_serial(noise + " extra") is None


def test_verifier_ignores_a_timeline_line_still_being_written(tmp_path):
    (tmp_path / "timeline.jsonl").write_text('{"sequence": 1}\n{"sequence": 2, "mod')
    assert records(tmp_path) == [{"sequence": 1}]


def test_exported_libraries_are_used_by_pin_without_git(tmp_path, monkeypatch):
    manifest = json.loads((build.ROOT / "native/libraries.json").read_text())
    for library in manifest["libraries"]:
        slug = build.re.sub(r"[^A-Za-z0-9]+", "-", library["name"]).strip("-").lower()
        path = tmp_path / f"{slug}-{library['commit']}"
        path.mkdir()
        (path / build.PIN).write_text(library["commit"] + "\n")

    def no_fetch(*args, **kwargs):
        raise AssertionError("pinned exports must not be fetched")

    monkeypatch.setattr(build, "fetch", no_fetch)
    resolved = build.libraries(tmp_path)
    assert [r["path"].parent for r in resolved] == [tmp_path] * 3
    # A pin for another commit is not trusted: the library would be fetched again.
    first = resolved[0]["path"] / build.PIN
    first.write_text("0" * 40 + "\n")
    with pytest.raises(AssertionError, match="must not be fetched"):
        build.libraries(tmp_path)


def test_live_timeline_does_not_parse_a_suffix_appended_after_a_partial_read(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / "timeline.jsonl"
    path.write_text('{"sequence":1}\n{"sequence":')
    original_open = Path.open
    appended = False

    class AppendingReader:
        def __init__(self):
            self.stream = original_open(path)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def append_after_read(self, data):
            nonlocal appended
            if not appended and data.endswith('{"sequence":'):
                with original_open(path, "a") as writer:
                    writer.write("2}\n")
                appended = True
            return data

        def read(self):
            return self.append_after_read(self.stream.read())

        def __iter__(self):
            return self

        def __next__(self):
            line = self.stream.readline()
            if not line:
                raise StopIteration
            return self.append_after_read(line)

    monkeypatch.setattr(Path, "open", lambda self, *args, **kwargs: AppendingReader())
    assert records(tmp_path) == [{"sequence": 1}]
    assert appended
    assert records(tmp_path) == [{"sequence": 1}, {"sequence": 2}]
