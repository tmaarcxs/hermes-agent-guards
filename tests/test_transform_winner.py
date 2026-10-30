"""Pin the joined transform behaviour: the skill-size flag survives the host pipeline.

The host keeps the FIRST non-None string a ``transform_tool_result`` callback
returns and drops the rest (``model_tools.py`` ``_apply_transform_tool_result_hook``:
``next((r for r in hook_results if isinstance(r, str)), result)``). Callbacks do
NOT chain: every callback receives the ORIGINAL result, and callback order is
plugin load order — alphabetical by plugin key with ``requires_plugins`` edges
first (``hermes_cli/plugins_manifest.py`` ``resolve_plugin_load_order``).

``kanban-guards`` loads before ``shell-guards``. Historically both produced a
``transform_tool_result`` string on a Kanban lead's threshold-crossing
``terminal`` call, and the flag lost the first-string race. The fix joins them:
``shell-guards`` delivers the skill-size flag on ``transform_terminal_output``,
which the terminal tool runs INSIDE the tool (``tools/terminal_tool_result.py``
``finalize_foreground_result``) BEFORE the host runs ``transform_tool_result``.
The flag is therefore already part of the result the later seam receives.

The delegation nudge that used to be the competing string was removed on
2026-10-30 (341 historical nudges, 7.9% conversion, 45% firing on system
notifications), so the race it caused no longer exists; the tests below keep
pinning the flag's path through both seams, which is the behaviour that must
survive any future transform added to either plugin.

Every test here drives the callbacks the plugins ACTUALLY registered (captured
from ``register()``) through the host's own resolution rules.
"""

import pytest

from _plugin_loader import load_plugin, ROOT

PLUGINS = ("kanban-guards", "shell-guards", "workspace-guards")

SMALL = "---\nname: demo\ndescription: Use when testing.\n---\n" + "x" * 100

FLAG = "over the limit for SKILL.md"


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
    """Both plugins loaded fresh, skill root redirected to tmp."""
    kanban = load_plugin("kanban-guards")
    shell = load_plugin("shell-guards")
    monkeypatch.setattr(kanban.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(shell.skill_size, "ROOTS", [skill_root])
    shell.skill_size._PENDING.clear()
    yield {
        "kanban-guards": kanban,
        "shell-guards": shell,
        "registered": {
            "kanban-guards": _registered_hooks(kanban),
            "shell-guards": _registered_hooks(shell),
        },
    }
    shell.skill_size._PENDING.clear()


def _arm_oversized_write(shell, skill_root, *, tool_call_id="c1"):
    """Snapshot the skill tree, then grow SKILL.md past its limit."""
    shell.skill_size.record("terminal", {}, tool_call_id=tool_call_id)
    skill = skill_root / "cat" / "demo" / "SKILL.md"
    skill.write_text(skill.read_text() + "y" * 7000)


def test_kanban_guards_loads_before_shell_guards():
    order = _host_load_order(list(PLUGINS))
    assert order.index("kanban-guards") < order.index("shell-guards"), (
        "kanban-guards must load before shell-guards so that, if it ever returns a "
        f"transform string again, it is the first the host keeps; host order was {order}"
    )


def test_shell_guards_registers_the_terminal_output_seam(plugins):
    """The flag's delivery depends on shell-guards registering ``transform_terminal_output``."""
    assert plugins["registered"]["shell-guards"].get("transform_terminal_output"), (
        "shell-guards must register transform_terminal_output; without it the "
        "skill-size flag rides only transform_tool_result and can lose to any other "
        "plugin's transform string"
    )


def test_skill_flag_only(plugins, skill_root):
    """An oversized write: the flag survives the whole pipeline."""
    _arm_oversized_write(plugins["shell-guards"], skill_root)

    final = _run_host_pipeline(plugins["registered"], _host_load_order(list(PLUGINS)), "BASE")
    assert FLAG in final and final.startswith("BASE")


def test_no_transform_string_without_a_flagged_file(plugins, skill_root):
    """A clean call leaves the result byte-identical: no plugin appends anything."""
    shell = plugins["shell-guards"]
    shell.skill_size.record("terminal", {}, tool_call_id="c1")

    final = _run_host_pipeline(plugins["registered"], _host_load_order(list(PLUGINS)), "BASE")
    assert final == "BASE"


def test_both_seams_carry_the_flag_exactly_once(plugins, skill_root):
    """The flag is delivered on the terminal seam and not duplicated on the result seam."""
    _arm_oversized_write(plugins["shell-guards"], skill_root)

    final = _run_host_pipeline(plugins["registered"], _host_load_order(list(PLUGINS)), "BASE")
    assert FLAG in final, "the skill-size flag was dropped from the joined result"
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
