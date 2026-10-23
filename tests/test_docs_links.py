"""Every relative link in the repository's Markdown files must resolve."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)|!\[[^\]]*\]\(([^)\s]+)\)")
SKIP_DIRS = {".git", ".venv", "build", "dist", "firmware", "runs", "node_modules"}


def _markdown_files():
    for path in sorted(ROOT.rglob("*.md")):
        if not SKIP_DIRS.intersection(path.relative_to(ROOT).parts):
            yield path


def _fenced_stripped(text):
    return re.sub(r"^```.*?^```", "", text, flags=re.S | re.M)


@pytest.mark.parametrize("doc", list(_markdown_files()), ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(doc):
    missing = []
    for match in LINK.finditer(_fenced_stripped(doc.read_text(encoding="utf-8"))):
        target = (match.group(1) or match.group(2)).split("#", 1)[0]
        if not target or re.match(r"[a-z][a-z0-9+.-]*:", target):
            continue
        base = ROOT if target.startswith("/") else doc.parent
        if not (base / target.lstrip("/")).exists():
            missing.append(target)
    assert not missing, f"{doc.relative_to(ROOT)} has broken links: {missing}"
