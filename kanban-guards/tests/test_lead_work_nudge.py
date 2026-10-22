"""The one-time lead nudge to split remaining work across parallel cards.

Behaviour pinned here: the counting rule (only exploration tools count; writers
set ``wrote`` and silence the turn), the per-user-turn scoping, the 6th-call
threshold and its one-shot nature, the ``kanban_create`` opt-out, the
worker-process exemption, the final nudge text, and the live ``PluginManager``
end-to-end shape.
"""

import sys

import pytest

guards = sys.modules["agent_guards"]
nudge = guards.lead_work_nudge

THRESHOLD = nudge.WORK_CALL_THRESHOLD


@pytest.fixture(autouse=True)
def lead_setup(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    nudge.reset()
    yield
    nudge.reset()


def transform(tool_name, result="ok", turn_id="turn-1", session_id="lead-session"):
    return guards._transform_tool_result(
        tool_name=tool_name, args={}, result=result, session_id=session_id, turn_id=turn_id)


def work_calls(count, turn_id="turn-1", tool_name="search_files"):
    return [transform(tool_name, turn_id=turn_id) for _ in range(count)]


def test_threshold_is_five_module_constant():
    assert THRESHOLD == 5


def test_first_five_calls_are_silent_and_the_sixth_nudges():
    for _ in range(THRESHOLD):
        assert transform("read_file") is None
    nudged = transform("terminal")
    assert nudged is not None and nudge.NUDGE.split("{count}")[0] in nudged


def test_nudge_fires_exactly_once_per_turn():
    work_calls(THRESHOLD)
    assert transform("terminal") is not None
    for _ in range(4):
        assert transform("read_file") is None


def test_counter_resets_at_a_new_turn():
    work_calls(THRESHOLD)
    assert transform("terminal") is not None
    assert transform("read_file", turn_id="turn-2") is None
    work_calls(THRESHOLD - 1, turn_id="turn-2")
    assert transform("terminal", turn_id="turn-2") is not None


def test_counter_is_per_session():
    work_calls(THRESHOLD, turn_id="turn-1")
    assert transform("terminal", turn_id="turn-1", session_id="other-session") is None


def test_kanban_create_before_threshold_disarms_the_nudge():
    transform("kanban_create")
    work_calls(THRESHOLD + 3)
    assert transform("terminal") is None


def test_kanban_create_after_threshold_does_not_retract_a_fired_nudge():
    work_calls(THRESHOLD)
    assert transform("terminal") is not None
    transform("kanban_create")
    assert transform("read_file") is None


@pytest.mark.parametrize(
    "tool_name",
    nudge.WORK_TOOLS,
)
def test_work_calls_count(tool_name):
    assert nudge.counts_as_work(tool_name)


@pytest.mark.parametrize(
    "tool_name",
    ["kanban_create", "kanban_show", "kanban_complete", "clarify", "memory", "skill_view",
     "skills_list", "skill_manage", "todo", "todo_list", "hindsight_recall", "process_manage",
     "vision_analyze", "execute_code", "tool_describe", "tool_search", "some_browser_tool", ""],
)
def test_coordination_calls_do_not_count(tool_name):
    assert not nudge.counts_as_work(tool_name)


def test_writes_do_not_count():
    work_calls(THRESHOLD)
    for tool in ("patch", "write_file"):
        before = nudge._bucket(("lead-session", "turn-1"))["calls"]
        assert transform(tool) is None
        assert nudge._bucket(("lead-session", "turn-1"))["calls"] == before


def test_a_turn_with_a_write_never_nudges():
    transform("patch")
    work_calls(THRESHOLD + 3)
    assert transform("terminal") is None
    nudge.reset()
    transform("write_file", turn_id="turn-2")
    work_calls(THRESHOLD + 3, turn_id="turn-2")
    assert transform("terminal", turn_id="turn-2") is None


def test_coordination_calls_never_reach_the_threshold():
    for _ in range(THRESHOLD + 3):
        assert transform("todo_list") is None
        assert transform("kanban_show") is None
        assert transform("skill_view") is None


def test_worker_process_is_never_counted_or_nudged(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_123")
    work_calls(THRESHOLD + 3)
    assert transform("terminal") is None


def test_without_a_configured_worker_profile_nothing_fires(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")
    work_calls(THRESHOLD + 3)
    assert transform("terminal") is None


def test_missing_turn_identity_leaves_the_result_untouched():
    work_calls(THRESHOLD)
    assert transform("terminal", turn_id="") is None


def test_non_string_result_passes_through():
    work_calls(THRESHOLD)
    assert transform("terminal", result={"error": "x"}) is None


def test_nudge_text_pins_the_split_instruction():
    work_calls(THRESHOLD)
    message = transform("terminal")
    assert "Split the remaining work across parallel workers" in message
    assert "one kanban_create card per independent part" in message
    assert "If what remains is a known change or something only you can do, continue." in message
    assert "You have made 6 exploration calls this turn without delegating." in message


def test_nudge_is_appended_to_the_untouched_result():
    work_calls(THRESHOLD)
    message = transform("terminal", result="COMMAND OUTPUT")
    assert message.startswith("COMMAND OUTPUT")
    assert nudge.NUDGE.split("{count}")[0].strip() in message


def test_live_plugin_manager_end_to_end(monkeypatch):
    hermes_cli = pytest.importorskip("hermes_cli.plugins")
    try:
        from hermes_cli.plugins_manifest import PluginManifest
    except Exception:
        pytest.skip("hermes_cli.plugins_manifest is unavailable")

    import os

    import hermes_cli.plugins as host_plugins

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    manifest = PluginManifest(name="agent-guards", version="test", source="user", path=repo_root)
    manager = host_plugins.PluginManager(scope_key="lead-nudge-e2e")
    ctx = host_plugins.PluginContext(manifest, manager)
    guards.register(ctx)

    def call(tool_name, turn_id="lead-turn-1", result="ok"):
        results = manager.invoke_hook(
            "transform_tool_result", tool_name=tool_name, args={}, result=result,
            session_id="lead-session", task_id="", tool_call_id="", turn_id=turn_id,
            api_request_id="", duration_ms=1, status="ok", error_type=None, error_message=None,
        )
        return next((r for r in results if isinstance(r, str)), result)

    for _ in range(THRESHOLD):
        assert call("read_file") == "ok"
    assert "kanban_create" in call("terminal")
    assert call("read_file") == "ok"

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_e2e")
    nudge.reset()
    for _ in range(THRESHOLD + 3):
        assert call("read_file", turn_id="worker-turn") == "ok"
