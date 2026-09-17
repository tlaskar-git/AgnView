"""AgnView is free and local. Nothing in the repository may say otherwise.

There is no paid tier, no paywall and no purchase path, so no wording may
imply one. This guards that, because monetisation copy tends to creep back in
through UI strings and marketing sections of the README.

Wording about the user's OWN third party AI subscriptions, such as Claude Pro
or ChatGPT Plus quotas, is the product working as intended and is not matched
here.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN = [
    re.compile(r"AgnView\s+Pro", re.I),
    re.compile(r"\bpaywall\b", re.I),
    re.compile(r"\bStoreKit\b", re.I),
    re.compile(r"in[- ]app purchase", re.I),
    re.compile(r"\bfree trial\b", re.I),
    re.compile(r"\bupgrade to\s+(pro|premium|paid|plus)\b", re.I),
    re.compile(r"\bsubscribe now\b", re.I),
    re.compile(r"\bbuy now\b", re.I),
    re.compile(r"\$\s?\d+(?:\.\d{2})?\s*(?:/|per\s)\s*(?:mo|month|yr|year)", re.I),
    re.compile(r"\blicen[cs]e key\b", re.I),
]

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "docs/screenshots"}
SKIP_SUFFIX = {
    ".png", ".jpg", ".jpeg", ".ico", ".woff", ".woff2", ".ttf", ".db", ".pyc",
}


def _candidate_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if any(rel == d or rel.startswith(d + "/") for d in SKIP_DIRS):
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIX:
            continue
        if "vendor" in rel:
            continue
        # This file necessarily names the words it forbids.
        if path.resolve() == Path(__file__).resolve():
            continue
        yield path, rel


@pytest.mark.parametrize("pattern", FORBIDDEN, ids=lambda p: p.pattern)
def test_no_monetisation_wording(pattern):
    hits = []
    for path, rel in _candidate_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{rel}:{number}: {line.strip()[:120]}")

    assert not hits, "monetisation wording found:\n" + "\n".join(hits)
