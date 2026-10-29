"""Shared predicates for the recall gates.

``is_lead`` is the same test the Kanban guards use: a process with no Kanban task
pin and a worker profile configured is a lead surface. Workers answer to their
card and keep their own memory path, so the recall gates stay out of them.
"""

import os
from typing import Any, Optional


def _config_value(path: str, default: Any = None) -> Any:
    try:
        from hermes_cli.config import load_config_readonly

        node: Any = load_config_readonly() or {}
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node
    except Exception:
        return default


def configured_worker() -> Optional[str]:
    """The configured Kanban worker profile name, or None when there is none."""
    value = _config_value("kanban.default_assignee")
    name = str(value or "").strip()
    return name or None


def is_lead() -> bool:
    """True in a lead process: no task pin and a Kanban worker profile configured."""
    if (os.environ.get("HERMES_KANBAN_TASK") or "").strip():
        return False
    return bool(configured_worker())


__all__ = ["configured_worker", "is_lead"]
