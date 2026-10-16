"""Fail when any repository .py file carries a comment-only line.

The plugin ships with a no-comment-lines convention: rationale lives in
docstrings, never in ``#`` lines. One test walks the repository root (every
``*.py`` file, skipping ``.git``, ``__pycache__`` and ``.venv`` directories)
and lists each offender with its line number; inside a clone the walk equals
the tracked tree, and a docstring is never a violation. Import-light by
design: stdlib only.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SKIP_DIRS = {".git", "__pycache__", ".venv", ".pytest_cache"}


def _comment_only_lines(path: Path) -> list:
    """1-based line numbers of comment-only lines outside any string literal."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    spans = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.lineno != node.end_lineno
        ):
            spans.append((node.lineno, node.end_lineno))
    found = []
    for number, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        if not stripped.startswith("#") or stripped.startswith("#!"):
            continue
        if any(start <= number <= end for start, end in spans):
            continue
        found.append(number)
    return found


def _repository_python_files() -> list:
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        found.append(path)
    return found


def test_no_comment_only_lines_in_any_repository_python_file():
    offenders = []
    for path in _repository_python_files():
        numbers = _comment_only_lines(path)
        if numbers:
            offenders.append(f"{path.relative_to(ROOT)}: lines {numbers}")
    assert offenders == [], (
        "comment-only lines are not allowed in this plugin; move the note "
        "into a docstring: " + "; ".join(offenders)
    )
