"""Fail when any tracked file carries an employer identifier the public repo must not expose.

``tmaarcxs/hermes-agent-guards`` is a public repository. A versioned guard keeps
personal absolute paths, employer names and internal project names out of it:
this test walks the tree and lists every ``file:line`` matching the same regex
the pre-push hook (``tools/check_privacy.sh``) uses, so a leaked identifier
fails the root suite even when the local hook is not installed. Import-light by
design: stdlib only.

The pattern is assembled from fragments so this file and the hook do not match
themselves, which would otherwise make the guard reject its own installation.

``LEGACY_LINE_HASHES`` froze the exact offending lines that shipped before the
guard existed, so the suite stayed green while those files were scrubbed. The
scrub is done and the set is empty; the mechanism stays for the next pre-guard
migration. Each entry is a hash of ``path + NUL + line``, so no identifier
appears here, and a NEW offending line hashes differently and still fails.
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

LEGACY_LINE_HASHES: set[str] = set()


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
