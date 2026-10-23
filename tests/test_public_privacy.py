"""Fail when any tracked file carries an employer identifier the public repo must not expose.

``tmaarcxs/hermes-agent-guards`` is a public repository. A versioned guard keeps
personal absolute paths, employer names and internal project names out of it:
this test walks the tree and lists every ``file:line`` matching the same regex
the pre-push hook (``tools/check_privacy.sh``) uses, so a leaked identifier
fails the root suite even when the local hook is not installed. Import-light by
design: stdlib only.

The pattern is assembled from fragments so this file and the hook do not match
themselves, which would otherwise make the guard reject its own installation.

``LEGACY_LINE_HASHES`` freezes the exact offending lines that already shipped
before the guard existed, so the suite stays green while those files are tracked
for scrubbing by a separate card. Each entry is a hash of ``path + NUL + line``,
so no identifier appears here, and a NEW offending line inside one of the frozen
files hashes differently and still fails the run.
"""

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv"}

_PAIRS = [
    ("indi", "tex"),
    ("marcos", "srud"),
    ("msan", "chez"),
    ("ext.indi", "tex.com"),
    ("marcos.san", "chez@"),
    ("marcos.san", "chez/"),
    ("audit", "qc"),
    ("sustaud", "qc"),
    ("logistica", "arteixo"),
    ("ka", "ia"),
    ("/Users/marcos.san", "chez"),
]
PRIVACY_RE = re.compile("|".join(a + b for a, b in _PAIRS), re.IGNORECASE)

LEGACY_LINE_HASHES = {
    "00909d42a133735a",
    "3623d3d4d990a0fa",
    "9f86a4c70afb46af",
    "b907ea75ba74fd42",
    "399613ab7ffaf761",
    "9dd78cd47f1250ae",
    "461a9fedd1edf13e",
    "baadfe2b2a655f74",
    "a807d9da12de948b",
    "da61bac4091e0d5c",
    "8e6ef4b3ea2976c6",
    "ce8fe163d1a2d587",
    "9e01bff9732c1bb6",
    "3ce4073ab4eadc34",
    "616bf8b5ca7be943",
    "5b048085aaf1cef3",
    "286bf1a4660cc479",
    "f253724dce999637",
    "046935e24435a8ce",
    "a7dde1fd475db877",
    "e759a68646425112",
    "de4d5f0ebf91daba",
    "181224aef9172abc",
    "c273bc39d7ea0311",
    "c428d3439c44a7cc",
    "a355c347f5acd7de",
    "235dfaf2783fceeb",
}


def _line_hash(name: str, stripped: str) -> str:
    return hashlib.sha256((name + "\x00" + stripped).encode()).hexdigest()[:16]


def _offenders() -> list:
    found = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if any(part in _SKIP_DIRS for part in relative.parts):
            continue
        name = relative.as_posix()
        if name == "tests/test_public_privacy.py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if not PRIVACY_RE.search(line):
                continue
            if _line_hash(name, line.strip()) in LEGACY_LINE_HASHES:
                continue
            found.append(f"{name}:{number}")
    return found


def test_no_employer_identifiers_in_tracked_files():
    offenders = _offenders()
    assert offenders == [], (
        "employer identifiers in the public repository: " + "; ".join(offenders)
    )
