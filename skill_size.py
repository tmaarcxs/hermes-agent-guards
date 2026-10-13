"""Keep skill files under a size limit so skills stay a small index plus a reference tree.

``check`` runs on ``pre_tool_call`` for ``skill_manage``, ``write_file`` and ``patch``
(replace mode). It computes the size each skill markdown file would have after the
call and blocks when that size is over the limit and larger than the current file:
``SKILL.md`` may hold at most ``SKILL_LIMIT`` characters and any other ``.md`` file
inside a skill at most ``REFERENCE_LIMIT``. Shrinking an over-limit file is always
allowed, so an oversized skill can be split step by step.

Only markdown inside the skill roots is measured; scripts and data files are not.
Unified-diff ``patch`` calls and writes through ``terminal``/``execute_code`` are not
measured (ponytail: catches slips, not deliberate evasion; ``skill_tree_check.py`` in
skill-library-audit audits the whole library).
"""

import os
from pathlib import Path
from typing import Any, Optional

SKILL_LIMIT = 6000
REFERENCE_LIMIT = 12000


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


_HOME = _hermes_home()
ROOTS = [_HOME / "skills", Path.home() / ".agents" / "skills", *(_HOME / "profiles").glob("*/skills")]
_SKIPPED_PARTS = {".archive", ".curator_backups", ".hub", ".locks"}


def _roots() -> list[Path]:
    return [Path(os.path.realpath(root)) for root in ROOTS if root.exists()]


def _limit(path: Path) -> Optional[int]:
    if path.suffix != ".md" or _SKIPPED_PARTS & set(path.parts):
        return None
    real = Path(os.path.realpath(path))
    if not any(real.is_relative_to(root) for root in _roots()):
        return None
    return SKILL_LIMIT if path.name == "SKILL.md" else REFERENCE_LIMIT


def _size(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return 0


def _skill_dir(name: str) -> Optional[Path]:
    for root in _roots():
        direct = root / name
        if (direct / "SKILL.md").is_file():
            return direct
        for skill_md in root.rglob("SKILL.md"):
            if skill_md.parent.name == name and not _SKIPPED_PARTS & set(skill_md.parts):
                return skill_md.parent
    return None


def _replaced(current: int, old: Any, new: Any, replace_all: bool, text: str) -> int:
    old, new = str(old or ""), str(new or "")
    count = text.count(old) if replace_all and old else 1
    return current + (len(new) - len(old)) * max(count, 1)


def _skill_manage_targets(args: dict) -> list[tuple[Path, int, int]]:
    operations = args.get("operations")
    if not isinstance(operations, list):
        operations = [args]
    simulated: dict[Path, int] = {}
    results: list[tuple[Path, int, int]] = []
    for op in operations:
        if not isinstance(op, dict):
            continue
        action = op.get("action") or args.get("action")
        name = str(op.get("name") or args.get("name") or "")
        if action not in ("create", "edit", "patch", "write_file") or not name:
            continue
        skill_dir = _skill_dir(name)
        if skill_dir is None:
            base = _roots()[0] if _roots() else _HOME / "skills"
            skill_dir = base / name
        file_path = op.get("file_path") if action in ("patch", "write_file") else None
        target = skill_dir / (file_path or "SKILL.md")
        before = simulated.get(target, _size(target))
        if action == "write_file":
            after = len(str(op.get("file_content") or ""))
        elif action == "patch" and not op.get("content"):
            text = target.read_text(encoding="utf-8") if target.is_file() else ""
            after = _replaced(before, op.get("old_string"), op.get("new_string"), bool(op.get("replace_all")), text)
        else:
            after = len(str(op.get("content") or ""))
        simulated[target] = after
        results.append((target, before, after))
    return results


def _file_tool_targets(tool_name: str, args: dict) -> list[tuple[Path, int, int]]:
    raw = args.get("path")
    if not raw:
        return []
    target = Path(os.path.expanduser(str(raw)))
    before = _size(target)
    if tool_name == "write_file":
        return [(target, before, len(str(args.get("content") or "")))]
    if (args.get("mode") or "replace") != "replace" or not target.is_file():
        return []
    text = target.read_text(encoding="utf-8")
    return [(target, before, _replaced(before, args.get("old_string"), args.get("new_string"),
                                       bool(args.get("replace_all")), text))]


def check(tool_name: str, args: Any = None, **_kwargs) -> Optional[str]:
    if not isinstance(args, dict):
        return None
    if tool_name == "skill_manage":
        targets = _skill_manage_targets(args)
    elif tool_name in ("write_file", "patch"):
        targets = _file_tool_targets(tool_name, args)
    else:
        return None
    for target, before, after in targets:
        limit = _limit(target)
        if limit is None or after <= limit or after < before:
            continue
        kind = "SKILL.md" if limit == SKILL_LIMIT else "a skill reference file"
        return (
            f"agent-guards blocked {tool_name}: {target.name} would be {after:,} characters; the limit for {kind} "
            f"is {limit:,} (SKILL.md {SKILL_LIMIT:,}, other skill .md files {REFERENCE_LIMIT:,}). Distribute the "
            "content as a tree instead: keep SKILL.md as the index (frontmatter, when to use, the few rules needed on "
            "every load, and one line per reference saying when to read it); put each topic in "
            "references/<topic>.md; when a reference outgrows its limit, keep that file as a short index pointing to "
            "references/<topic>-<subtopic>.md files. Move text verbatim, keep existing file paths, then retry."
        )
    return None
