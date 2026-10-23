"""Every registered pre_tool_call check stamps its own name on the block message.

Pre-split this lived in ``tests/test_block_attribution.py`` and parametrized over
one 14-name list, because all guards shared one ``_pre_tool_call``. After the
split each plugin owns its own tuple, so this walks all three plugins' declared
check lists and pins the same contract per plugin: a blocked call's message reads
``agent-guards blocked <tool> [<guard>]: ...`` — the census scripts match the
exact ``agent-guards blocked `` prefix, so the guard name goes AFTER the tool
token, never before it. A raising guard keeps the chain fail-open.
"""

import pytest

from _plugin_loader import load_plugin

CHECK_TUPLES = {
    "shell-guards": ("paas_promote_gate", "skill_size", "background_notify_gate"),
    "workspace-guards": ("workspace_write_gate", "home_litter_gate", "workspace_convention"),
    "kanban-guards": (
        "checkpoint_gate", "kanban_policy", "github_contact",
        "review_gate", "handoff_gate", "completion_gate", "backup_copy_gate", "schedule_gate",
    ),
}


def _policy_of(module):
    return getattr(module, "_policy", None) or getattr(module, "kanban_policy", None)


def _stub_others(monkeypatch, module, names, keep):
    for name in names:
        if name == keep:
            continue
        guard = getattr(module, name, None)
        if guard is not None and hasattr(guard, "check"):
            monkeypatch.setattr(guard, "check", lambda *a, **k: None)


_PAIRS = [(folder, name) for folder, names in CHECK_TUPLES.items() for name in names]


@pytest.mark.parametrize("folder_name", sorted(CHECK_TUPLES))
def test_declared_check_names_match_the_package(folder_name):
    module = load_plugin(folder_name)
    for name in CHECK_TUPLES[folder_name]:
        assert getattr(module, name, None) is not None, (
            f"{folder_name} declares check '{name}' but the package has no such module"
        )


@pytest.mark.parametrize("folder_name,name", _PAIRS)
def test_block_from_each_guard_carries_its_name(folder_name, name, monkeypatch):
    module = load_plugin(folder_name)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    policy = _policy_of(module)
    if policy is not None and hasattr(policy, "configured_worker"):
        monkeypatch.setattr(policy, "configured_worker", lambda: "")
    _stub_others(monkeypatch, module, CHECK_TUPLES[folder_name], name)
    monkeypatch.setattr(getattr(module, name), "check", lambda *a, **k: "agent-guards blocked x: y")
    result = module._pre_tool_call(tool_name="x", args={}, session_id=f"s-attr-{name}")
    assert result is not None and result["action"] == "block", result
    assert result["message"] == f"agent-guards blocked x [{name}]: y"


def test_terminal_block_keeps_the_audit_prefix():
    module = load_plugin("shell-guards")
    result = module._pre_tool_call(tool_name="terminal", args={"command": "sleep 60", "background": False})
    assert result["action"] == "block"
    assert result["message"].startswith("agent-guards blocked terminal")
    assert "[__init__]" in result["message"]


@pytest.mark.parametrize("folder_name", sorted(CHECK_TUPLES))
def test_raising_guard_stays_fail_open(folder_name, monkeypatch):
    module = load_plugin(folder_name)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    policy = _policy_of(module)
    if policy is not None and hasattr(policy, "configured_worker"):
        monkeypatch.setattr(policy, "configured_worker", lambda: "")

    def boom(*a, **k):
        raise RuntimeError("probe crash")

    first = CHECK_TUPLES[folder_name][0]
    _stub_others(monkeypatch, module, CHECK_TUPLES[folder_name], first)
    monkeypatch.setattr(getattr(module, first), "check", boom)
    assert module._pre_tool_call(tool_name="x", args={}, session_id=f"s-attr-raise-{folder_name}") is None
