"""The plugin registers exactly what plugin.yaml declares, in the pre-split order.

Acceptance 3 of the kanban-guards split card. The order source is root
``agent-guards/__init__.py`` at base 90456b2:
- ``register`` (lines 160-165) wires ``pre_tool_call``, ``post_tool_call``,
  ``pre_verify`` (stop_gate.check), ``transform_tool_result`` and wakes;
- the ``pre_tool_call`` tuple (lines 274-291) lists the checks in one fixed order;
  this plugin keeps only the Kanban checks, in the same relative order:
  checkpoint_gate, kanban_policy, lead_delegation, github_contact, review_gate,
  handoff_gate, completion_gate, backup_copy_gate, schedule_gate (the shell-side
  entries — workspace_write_gate, home_litter_gate, workspace_convention,
  paas_promote_gate, skill_size, background_notify_gate — belong to other plugins);
- the ``transform_tool_result`` chain (lines 233-265) keeps completion_gate,
  workspace_scope_gate, kanban_show_slim_gate, then the lead nudge last; the root's
  ``skill_size.transform`` entry belongs to shell-guards.
"""

import sys

import yaml

import conftest

guards = sys.modules["kanban_guards"]


def _declared_hooks():
    data = yaml.safe_load((_root_yaml := conftest._root / "plugin.yaml").read_text())
    return tuple(data["hooks"])


def _registered_hooks():
    registered = []

    class Ctx:
        def register_hook(self, hook, fn):
            registered.append(hook)

    guards.register(Ctx())
    return registered


def test_register_matches_declared_hooks():
    declared = _declared_hooks()
    registered = _registered_hooks()
    assert set(registered) == set(declared), (registered, declared)


def test_pre_tool_call_check_order_matches_root_at_90456b2():
    """Kanban-order pin, derived from the root tuple at 90456b2: keep the relative
    order of the checks this plugin owns, dropping the shell-owned entries."""
    assert list(guards._PRE_TOOL_CALL_CHECK_ORDER) == [
        "checkpoint_gate",
        "kanban_policy",
        "lead_delegation",
        "github_contact",
        "pr_link_gate",
        "review_gate",
        "handoff_gate",
        "completion_gate",
        "backup_copy_gate",
        "schedule_gate",
    ]


def test_transform_chain_order_matches_root_at_90456b2():
    """completion -> scope -> show-slim -> nudge, same as the root chain."""
    import inspect

    source = inspect.getsource(guards._transform_tool_result)
    positions = [
        source.index("completion_gate.transform"),
        source.index("workspace_scope_gate.transform"),
        source.index("kanban_show_slim_gate.transform"),
        source.index("lead_work_nudge.hook"),
    ]
    assert positions == sorted(positions)
