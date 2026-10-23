"""Parse the worker's persisted ``todo_list`` tool result and map it to a progress line.

Deterministic and total: malformed input never raises, it returns ``None`` so the
caller writes nothing.

The persisted payload (``tools/todo_tool.py``) is::

    {"todos": [{"id": str, "content": str, "status": str, "parent"?: str}, ...],
     "revision": int, "summary": {"total": int, ...}}

``status`` is one of ``pending``, ``in_progress``, ``completed``, ``cancelled``.
"""

from __future__ import annotations

import json
from typing import Any, Optional

VALID_STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled"})

# A todo result larger than this is a forged/oversized row (tools/todo_tool.py
# MAX_TODO_RESULT_CHARS); the SQL already filters on it, this is belt and braces.
MAX_TODO_RESULT_CHARS = 512_000


class Progress:
    """The mapped progress of one Tasklist: ``N/M``, a label and a percent."""

    __slots__ = ("done", "total", "label", "percent")

    def __init__(self, done: int, total: int, label: str) -> None:
        self.done = done
        self.total = total
        self.label = label
        self.percent = round(100 * done / total) if total else 0

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Progress)
            and (self.done, self.total, self.label) == (other.done, other.total, other.label)
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Progress(done={self.done}, total={self.total}, label={self.label!r})"


def parse_todos(content: Optional[str]) -> Optional[list[dict[str, Any]]]:
    """Strictly parse a persisted ``todo_list`` result into a list of todo dicts.

    Returns ``None`` when the payload is not a JSON object with a ``todos`` list.
    Malformed individual items are dropped rather than failing the whole row; an
    item is malformed when it is not a dict, has no usable ``content``/``status``,
    or carries a status outside :data:`VALID_STATUSES`.
    """
    if not content:
        return None
    if len(content) > MAX_TODO_RESULT_CHARS:
        return None
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("todos")
    if not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if not isinstance(status, str) or status not in VALID_STATUSES:
            continue
        text = item.get("content")
        if text is None:
            text = item.get("title") or item.get("text")
        if not isinstance(text, str):
            continue
        entry: dict[str, Any] = {"content": text, "status": status}
        item_id = item.get("id")
        if isinstance(item_id, str):
            entry["id"] = item_id
        out.append(entry)
    return out


def map_progress(todos: Optional[list[dict[str, Any]]]) -> Optional[Progress]:
    """Map a parsed Tasklist to :class:`Progress`, or ``None`` when there is nothing to show.

    ``N`` counts ``completed`` items, ``M`` counts everything except ``cancelled``,
    so the bar tracks work that still matters. The label is the first
    ``in_progress`` item, else the first ``pending`` item, else the last
    ``completed`` item (the format module cuts it by the measured PIXEL budget,
    on a word boundary when possible, never with an ellipsis).

    Returns ``None`` for a missing list, an empty list, or ``M == 0`` — in those
    cases the caller writes nothing and leaves any existing block untouched.
    """
    if not todos:
        return None
    counted = [t for t in todos if t["status"] != "cancelled"]
    total = len(counted)
    if total == 0:
        return None
    done = sum(1 for t in counted if t["status"] == "completed")
    label = _pick_label(counted)
    return Progress(done, total, label)


def _pick_label(todos: list[dict[str, Any]]) -> str:
    for wanted in ("in_progress", "pending"):
        for item in todos:
            if item["status"] == wanted:
                return item["content"]
    completed = [t for t in todos if t["status"] == "completed"]
    return completed[-1]["content"] if completed else ""


def progress_from_content(content: Optional[str]) -> Optional[Progress]:
    """``todo_list`` result JSON -> :class:`Progress` (or ``None``). Never raises."""
    try:
        return map_progress(parse_todos(content))
    except Exception:  # defensive: a mapping bug must not kill the sweep
        return None
