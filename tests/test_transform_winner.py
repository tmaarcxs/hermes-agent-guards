"""Pin the joined transform behaviour: the nudge and the skill-size flag both survive.

The host keeps the FIRST non-None string a ``transform_tool_result`` callback
returns and drops the rest (``model_tools.py`` ``_apply_transform_tool_result_hook``:
``next((r for r in hook_results if isinstance(r, str)), result)``). Callbacks do
NOT chain: every callback receives the ORIGINAL result, and callback order is
plugin load order — alphabetical by plugin key with ``requires_plugins`` edges
first (``hermes_cli/plugins_manifest.py`` ``resolve_plugin_load_order``).

With the three-plugin split that makes ``kanban-guards`` load before
``shell-guards``, so a Kanban lead's threshold-crossing ``terminal`` call that
ALSO left an oversized skill file used to let the nudge win and silently drop the
flag. The fix joins them: ``shell-guards`` delivers the skill-size flag on
``transform_terminal_output``, which the terminal tool runs INSIDE the tool
(``tools/terminal_tool_result.py`` ``finalize_foreground_result``) BEFORE the host
runs ``transform_tool_result``. The flag is therefore already part of the result
the ``kanban-guards`` nudge appends to, and one ``transform_tool_result`` string
carries both messages.

Every test here drives the callbacks the plugins ACTUALLY registered (captured
from ``register()``) through the host's own resolution rules, so the pre-join head
fails on the real defect: the flag is dropped from the joined result.
"""

import pytest

from _plugin_loader import load_plugin, ROOT

PLUGINS = ("kanban-guards", "shell-guards", "workspace-guards")

SMALL = "---\nname: demo\ndescription: Use when testing.\n---\n" + "x" * 100

FLAG = "over the limit for SKILL.md"
NUDGE = "Split the remaining work across parallel workers"


def _host_load_order(names):
    """The host's order: requires_plugins edges first, alphabetical ties."""
    try:
        from hermes_cli.plugins_manifest import PluginManifest, resolve_plugin_load_order

        manifests = {
            name: PluginManifest(name=name, version="test", source="user", path=str(ROOT / name))
            for name in names
        }
        return list(resolve_plugin_load_order(manifests))
    except Exception:
        return sorted(names)


def _registered_hooks(module):
    """The ``{hook_name: [callback, ...]}`` a plugin's ``register()`` actually wires."""
    hooks = {}

    class _Ctx:
        def register_hook(self, name, callback):
            hooks.setdefault(name, []).append(callback)

    module.register(_Ctx())
    return hooks


def _run_host_pipeline(registered, order, output, *, tool_name="terminal", args=None,
                       tool_call_id="c1", session_id="lead-session", turn_id="turn-1"):
    """The host's two seams, in the host's order.

    First ``transform_terminal_output`` (inside the terminal tool), then
    ``transform_tool_result`` (after the tool returns). Each seam keeps the FIRST
    non-None string and does not chain.
    """
    args = args if args is not None else {}
    terminal_results = []
    for name in order:
        for callback in registered.get(name, {}).get("transform_terminal_output", []):
            terminal_results.append(callback(
                command="cat >> x", output=output, returncode=0, task_id="",
                env_type="local", tool_call_id=tool_call_id))
    output = next((r for r in terminal_results if isinstance(r, str)), output)

    result_results = []
    for name in order:
        for callback in registered.get(name, {}).get("transform_tool_result", []):
            result_results.append(callback(
                tool_name=tool_name, args=args, result=output,
                session_id=session_id, tool_call_id=tool_call_id, turn_id=turn_id))
    return next((r for r in result_results if isinstance(r, str)), output)


@pytest.fixture
def skill_root(tmp_path):
    """A skill root holding one small SKILL.md the test can grow past the limit."""
    skills = tmp_path / "skills"
    (skills / "cat" / "demo" / "references").mkdir(parents=True)
    (skills / "cat" / "demo" / "SKILL.md").write_text(SMALL)
    return skills


@pytest.fixture
def plugins(skill_root, monkeypatch):
    """Both plugins loaded fresh, lead nudge armed, skill root redirected to tmp."""
    kanban = load_plugin("kanban-guards")
    shell = load_plugin("shell-guards")
    monkeypatch.setattr(kanban.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(shell.skill_size, "ROOTS", [skill_root])
    kanban.lead_work_nudge.reset()
    shell.skill_size._PENDING.clear()
    yield {
        "kanban-guards": kanban,
        "shell-guards": shell,
        "registered": {
            "kanban-guards": _registered_hooks(kanban),
            "shell-guards": _registered_hooks(shell),
        },
    }
    kanban.lead_work_nudge.reset()
    shell.skill_size._PENDING.clear()


def _arm_oversized_write(shell, skill_root, *, tool_call_id="c1"):
    """Snapshot the skill tree, then grow SKILL.md past its limit."""
    shell.skill_size.record("terminal", {}, tool_call_id=tool_call_id)
    skill = skill_root / "cat" / "demo" / "SKILL.md"
    skill.write_text(skill.read_text() + "y" * 7000)


def _cross_the_nudge_threshold(kanban):
    """Five exploration calls in one turn so the next one nudges."""
    for _ in range(kanban.lead_work_nudge.WORK_CALL_THRESHOLD):
        kanban._transform_tool_result(tool_name="read_file", args={}, result="ok",
                                      session_id="lead-session", turn_id="turn-1")


def test_kanban_guards_loads_before_shell_guards():
    order = _host_load_order(list(PLUGINS))
    assert order.index("kanban-guards") < order.index("shell-guards"), (
        "kanban-guards must load before shell-guards so its transform is the "
        f"first string the host keeps; host order was {order}"
    )


def test_shell_guards_registers_the_terminal_output_seam(plugins):
    """The join depends on shell-guards registering ``transform_terminal_output``."""
    assert plugins["registered"]["shell-guards"].get("transform_terminal_output"), (
        "shell-guards must register transform_terminal_output; without it the "
        "skill-size flag rides only transform_tool_result and loses to the nudge"
    )


def test_skill_flag_only(plugins, skill_root):
    """An oversized write with no threshold crossing: the flag survives alone."""
    _arm_oversized_write(plugins["shell-guards"], skill_root)

    final = _run_host_pipeline(plugins["registered"], _host_load_order(list(PLUGINS)), "BASE")
    assert FLAG in final and final.startswith("BASE")
    assert NUDGE not in final


def test_nudge_only(plugins, skill_root):
    """A threshold crossing that left no oversized file: the nudge survives alone."""
    shell = plugins["shell-guards"]
    shell.skill_size.record("terminal", {}, tool_call_id="c1")
    _cross_the_nudge_threshold(plugins["kanban-guards"])

    final = _run_host_pipeline(plugins["registered"], _host_load_order(list(PLUGINS)), "BASE")
    assert NUDGE in final and FLAG not in final


def test_both_messages_survive_in_a_stable_order(plugins, skill_root):
    """The corner case: one terminal call crosses the nudge threshold AND leaves a skill oversized.

    The flag is delivered on ``transform_terminal_output`` before the host runs
    ``transform_tool_result``, so the single string the host keeps carries both —
    exactly the OLD composition (flag first, nudge after). On the pre-join head the
    flag rode ``transform_tool_result``, lost the first-string race to the nudge,
    and was dropped here.
    """
    _arm_oversized_write(plugins["shell-guards"], skill_root)
    _cross_the_nudge_threshold(plugins["kanban-guards"])

    final = _run_host_pipeline(plugins["registered"], _host_load_order(list(PLUGINS)), "BASE")
    assert FLAG in final, "the skill-size flag was dropped from the joined result"
    assert NUDGE in final, "the delegation nudge was dropped from the joined result"
    assert final.index(FLAG) < final.index(NUDGE), (
        "the flag rides the tool result and the nudge appends after it, so the "
        f"flag must come first; got {final!r}"
    )


def test_one_call_is_flagged_once_across_both_seams(plugins, skill_root):
    """Both seams pop the same snapshot, so the flag is not appended twice."""
    _arm_oversized_write(plugins["shell-guards"], skill_root)

    final = _run_host_pipeline(plugins["registered"], _host_load_order(list(PLUGINS)), "BASE")
    assert final.count(FLAG) == 1, f"the flag must appear exactly once; got {final!r}"


def test_non_terminal_watched_tool_still_flags_on_the_result_seam(plugins, skill_root):
    """``execute_code`` has no terminal-output seam, so its flag rides ``transform_tool_result``."""
    shell = plugins["shell-guards"]
    shell.skill_size.record("execute_code", {}, tool_call_id="c2")
    (skill_root / "cat" / "demo" / "references" / "fresh.md").write_text("r" * 12001)

    final = _run_host_pipeline(plugins["registered"], _host_load_order(list(PLUGINS)), "BASE",
                               tool_name="execute_code", tool_call_id="c2")
    assert "over the limit for a skill reference file" in final


def test_non_string_output_leaves_the_terminal_seam_untouched(plugins, skill_root):
    """Fail open: a non-string output never changes the terminal result."""
    shell = plugins["shell-guards"]
    _arm_oversized_write(shell, skill_root)
    callback = plugins["registered"]["shell-guards"]["transform_terminal_output"][0]
    assert callback(command="cat >> x", output={"ok": True}, returncode=0, task_id="",
                    env_type="local", tool_call_id="c1") is None
