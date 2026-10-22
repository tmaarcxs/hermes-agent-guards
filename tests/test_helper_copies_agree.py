"""Fail when the duplicated shared-library helpers drift apart.

The monorepo decision (Marcos, 2026-10-22) makes each plugin folder
self-contained: no shared/common plugin, so the two small helpers every lane
needs travel as verbatim copies instead of one import.

- ``shell-guards/_policy.py``
- ``workspace-guards/_policy.py``
- ``kanban-guards/kanban_policy.py``

Duplication only stays safe while the copies are identical. This test compares
the ``RETRY_INSTRUCTION`` value, the ``configured_worker`` source and the
``configured_worker`` behaviour of all three copies, so any edit to one copy
that is not mirrored to the others fails here instead of silently forking the
config probe or the retry wording. When a copy legitimately must change, change
all three in the same commit.
"""

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

COPIES = {
    "shell-guards": ROOT / "shell-guards" / "_policy.py",
    "workspace-guards": ROOT / "workspace-guards" / "_policy.py",
    "kanban-guards": ROOT / "kanban-guards" / "kanban_policy.py",
}


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def _function_source(path: Path, name: str) -> str:
    """Normalized source of one top-level function, docstring included."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f"{name}() is missing from {path}")


@pytest.fixture(params=sorted(COPIES))
def copy(request):
    return request.param, COPIES[request.param]


def test_every_copy_exists(copy):
    name, path = copy
    assert path.is_file(), f"{name} helper copy is missing at {path}"


def test_retry_instruction_is_identical_everywhere(copy):
    name, path = copy
    module = _load(path, f"_helper_probe_retry_{name.replace('-', '_')}")
    baseline = _load(COPIES["kanban-guards"], "_helper_probe_retry_baseline")
    assert module.RETRY_INSTRUCTION == baseline.RETRY_INSTRUCTION, (
        f"{name} RETRY_INSTRUCTION drifted from the kanban-guards original; "
        "keep the three copies byte-identical or change all three together"
    )


def test_configured_worker_source_is_identical_everywhere(copy):
    name, path = copy
    assert _function_source(path, "configured_worker") == _function_source(
        COPIES["kanban-guards"], "configured_worker"
    ), (
        f"{name} configured_worker() source drifted from the kanban-guards "
        "original; the three copies must stay identical"
    )


@pytest.mark.parametrize(
    "config, expected",
    [
        ({"kanban": {"default_assignee": "worker"}}, "worker"),
        ({"kanban": {"default_assignee": "  worker  "}}, "worker"),
        ({"kanban": {"default_assignee": ""}}, ""),
        ({"kanban": {}}, ""),
        ({}, ""),
    ],
)
def test_configured_worker_behaves_the_same_everywhere(copy, config, expected):
    name, path = copy
    import hermes_cli.config as host_config

    original = host_config.load_config_readonly
    host_config.load_config_readonly = lambda: config
    try:
        module = _load(path, f"_helper_probe_behavior_{name.replace('-', '_')}")
        assert module.configured_worker() == expected
    finally:
        host_config.load_config_readonly = original


def test_all_three_copies_agree_on_the_same_probe(copy):
    name, path = copy
    import hermes_cli.config as host_config

    original = host_config.load_config_readonly
    host_config.load_config_readonly = lambda: {"kanban": {"default_assignee": "worker"}}
    try:
        mine = _load(path, f"_helper_probe_agree_{name.replace('-', '_')}").configured_worker()
        others = [
            _load(other, f"_helper_probe_agree_other_{key.replace('-', '_')}").configured_worker()
            for key, other in COPIES.items()
            if key != name
        ]
    finally:
        host_config.load_config_readonly = original
    assert mine == others[0] == others[1], f"{name} disagrees with the other copies: {mine!r} vs {others!r}"
