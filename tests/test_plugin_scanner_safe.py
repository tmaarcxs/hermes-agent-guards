"""Fail when the Hermes plugin scanner no longer rates this plugin tree ``safe``.

The Hermes gateway auto-applies plugin updates only when the staged candidate
tree scans ``safe``; a ``caution`` verdict raises the install gate to a
confirmation prompt, which the unattended path (``force=False``) cannot answer,
so the update is refused and the hourly check keeps reporting an available
update. A single ``high`` finding anywhere the scanner reads — including this
plugin's own README prose and test fixtures — is enough to downgrade the whole
tree from ``safe`` to ``caution``.

This test therefore runs the real scanner (``tools.plugin_guard.scan_plugin``,
shipped with the Hermes checkout) over the plugin root and asserts the verdict
stays ``safe``. When it fails, the assertion message lists every ``high`` and
``critical`` finding with its file and line so the offending prose can be
reworded; ``medium``/``low`` findings alone never change the verdict.

The suite skips when the Hermes checkout is unavailable (the scanner is a host
component, not part of this repository).
"""

import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _scan_plugin():
    module = importlib.import_module("tools.plugin_guard")
    return module.scan_plugin(ROOT)


def _blocking_findings(result):
    return [f for f in result.findings if f.severity in ("high", "critical")]


def test_plugin_tree_scans_safe_for_unattended_updates():
    try:
        result = _scan_plugin()
    except ImportError as exc:
        pytest.skip(f"Hermes scanner unavailable in this environment: {exc}")

    blocking = _blocking_findings(result)
    detail = "; ".join(
        f"{f.severity.upper()} {f.pattern_id} at {f.file}:{f.line} -> {f.match[:160]}"
        for f in blocking
    ) or "(none)"
    assert result.verdict == "safe" and not blocking, (
        "the Hermes plugin scanner rates this tree "
        f"'{result.verdict}' with {len(result.findings)} finding(s), so the "
        "unattended plugin update is refused. Reword the offending prose (the "
        "rule's meaning can stay) so no high/critical finding remains. "
        f"Blocking findings: {detail}"
    )
