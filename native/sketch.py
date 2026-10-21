"""Arduino sketch preprocessing and PlatformIO build-flag resolution for native builds.

The firmware is compiled as the Arduino builder would: the .ino gains `#include <Arduino.h>`
and prototypes for its top-level functions, with `#line` directives keeping diagnostics on the
original file. Source text is otherwise unchanged.
"""

import configparser
import re
import shlex
from pathlib import Path

KEYWORDS = {"if", "for", "while", "switch", "return", "catch", "sizeof", "do", "else"}
SIGNATURE = re.compile(
    r"(?P<head>(?:[A-Za-z_][\w:<>,]*[\s*&]+)+?)(?P<name>[A-Za-z_]\w*)\s*\((?P<args>[^()]*)\)"
    r"(?P<tail>\s*(?:const|noexcept)?\s*)$",
    re.DOTALL,
)


def _mask(text):
    """Blank comments, literals and preprocessor lines, keeping every offset."""
    out = list(text)
    i, length = 0, len(text)
    line_start = True
    while i < length:
        c = text[i]
        if line_start and c == "#":
            while i < length and text[i] != "\n":
                if text[i] == "\\" and i + 1 < length and text[i + 1] == "\n":
                    out[i] = " "
                    i += 2
                    continue
                out[i] = " "
                i += 1
            continue
        if c == "\n":
            line_start = True
            i += 1
            continue
        if not c.isspace():
            line_start = False
        if text.startswith("//", i):
            while i < length and text[i] != "\n":
                out[i] = " "
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = length if end < 0 else end + 2
            for j in range(i, end):
                if text[j] != "\n":
                    out[j] = " "
            i = end
        elif c in "\"'":
            j = i + 1
            while j < length and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            for k in range(i, min(j + 1, length)):
                if text[k] != "\n":
                    out[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(out)


def function_definitions(text):
    """(offset, prototype) for each top-level function definition, in source order."""
    masked = _mask(text)
    found = []
    braces = parens = 0
    statement = 0
    for index, c in enumerate(masked):
        if c == "(":
            parens += 1
        elif c == ")":
            parens -= 1
        elif c == "{":
            if braces == 0 and parens == 0:
                head = masked[statement:index]
                match = SIGNATURE.search(head.strip())
                if match and match["name"] not in KEYWORDS and "=" not in head:
                    words = match["head"].split()
                    if not (
                        {"struct", "class", "enum", "union", "namespace", "template"} & set(words)
                    ):
                        offset = statement + len(head) - len(head.lstrip())
                        prototype = " ".join(head.strip().split()) + ";"
                        found.append((offset, prototype))
            braces += 1
        elif c == "}":
            braces -= 1
            if braces == 0 and parens == 0:
                statement = index + 1
        elif c == ";" and braces == 0 and parens == 0:
            statement = index + 1
    if braces != 0 or parens != 0:
        raise ValueError("unbalanced braces or parentheses in sketch")
    return found


def preprocess_sketch(text, path):
    """Return the translation unit the Arduino builder would compile for this sketch."""
    definitions = function_definitions(text)
    quoted = str(path).replace("\\", "\\\\").replace('"', '\\"')
    if not definitions:
        return f'#include <Arduino.h>\n#line 1 "{quoted}"\n{text}'
    insert = text.rfind("\n", 0, definitions[0][0]) + 1
    line = text.count("\n", 0, insert) + 1
    prototypes = "\n".join(p for _, p in definitions)
    return (
        f'#include <Arduino.h>\n#line 1 "{quoted}"\n{text[:insert]}'
        f'{prototypes}\n#line {line} "{quoted}"\n{text[insert:]}'
    )


def _expand(value, config, depth=0):
    if depth > 10:
        raise ValueError("platformio.ini interpolation is too deep")

    def replace(match):
        section, _, option = match[1].rpartition(".")
        if not config.has_option(section, option):
            raise ValueError(f"platformio.ini references missing ${{{match[1]}}}")
        return _expand(config.get(section, option), config, depth + 1)

    return re.sub(r"\$\{([^}]+)\}", replace, value)


def _option(config, env, option, depth=0):
    section = f"env:{env}"
    if config.has_option(section, option):
        return config.get(section, option)
    if depth < 10 and config.has_option(section, "extends"):
        for parent in config.get(section, "extends").split(","):
            parent = parent.strip()
            name = parent.removeprefix("env:")
            if parent == "env" and config.has_option("env", option):
                return config.get("env", option)
            if parent != "env" and config.has_section(f"env:{name}"):
                value = _option(config, name, option, depth + 1)
                if value is not None:
                    return value
    return config.get("env", option) if config.has_option("env", option) else None


def platformio_env(path, env):
    """Board and -D defines of one PlatformIO environment (only defines may be native-built)."""
    config = configparser.ConfigParser(
        interpolation=None,
        comment_prefixes=(";", "#"),
        inline_comment_prefixes=(";",),
        strict=True,
    )
    config.optionxform = str
    config.read_string(Path(path).read_text())
    if not config.has_section(f"env:{env}"):
        raise ValueError(f"platformio.ini has no [env:{env}]")
    board = _option(config, env, "board")
    flags = _expand(_option(config, env, "build_flags") or "", config)
    defines = {}
    tokens = [t for line in flags.splitlines() for t in shlex.split(line)]
    iterator = iter(tokens)
    for token in iterator:
        if token == "-D":
            token = "-D" + next(iterator, "")
        if not token.startswith("-D"):
            raise ValueError(f"unsupported build flag for native builds: {token!r}")
        name, separator, value = token[2:].strip().partition("=")
        if not re.fullmatch(r"[A-Za-z_]\w*", name):
            raise ValueError(f"invalid define in build_flags: {token!r}")
        defines[name] = value if separator else "1"
    return {"env": env, "board": board, "defines": defines}
