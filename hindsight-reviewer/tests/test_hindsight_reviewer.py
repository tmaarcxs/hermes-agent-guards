"""The reviewer invalidates only a confirmed, quoted, unchanged primary fact and notifies once."""

import importlib
import importlib.util
import json
import sys
import types
import urllib.error
from collections import Counter
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parents[1]
if "hindsight_reviewer" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "hindsight_reviewer", _root / "__init__.py", submodule_search_locations=[str(_root)]
    )
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["hindsight_reviewer"] = _module
    _spec.loader.exec_module(_module)
reviewer = importlib.import_module("hindsight_reviewer.reviewer")
policy = reviewer.policy

FACT = "11111111-1111-1111-1111-111111111111"
OBS = "22222222-2222-2222-2222-222222222222"
USER = "We moved the staging database to port 6543 this morning."


class FakeLlm:
    def __init__(self, decision="candidate", category="other", quote="moved the staging database to port 6543", verdict="refutes"):
        self.decision, self.category, self.quote, self.verdict = decision, category, quote, verdict
        self.calls = []

    def complete_structured(self, instructions, input, json_schema, **kwargs):
        self.calls.append(kwargs)
        payload = json.loads(input[0]["text"])
        if "MEMORY" in payload:
            return types.SimpleNamespace(parsed={"verdict": self.verdict})
        if kwargs.get("purpose") == "Hindsight noise review":
            rows = [{"id": case["id"], "decision": "keep", "reason": "none", "duplicate_of": ""}
                    for case in payload["MEMORIES"]]
            return types.SimpleNamespace(parsed={"items": rows})
        rows = [{"id": case["id"], "decision": self.decision, "category": self.category,
                 "evidence_quote": self.quote if self.decision == "candidate" else ""} for case in payload["MEMORIES"]]
        return types.SimpleNamespace(parsed={"items": rows})


class FakeBank:
    def __init__(self, text="The staging database runs on port 5432.", edit_before_patch=None):
        self.rows = {OBS: {"type": "observation", "state": "valid", "text": "Staging DB is on 5432.", "source_memory_ids": [FACT]},
                     FACT: {"type": "world", "state": "valid", "text": text}}
        self.edit_before_patch = edit_before_patch
        self.patches = []
        self.gets = 0

    def __call__(self, method, url, key, body=None):
        memory_id = url.rsplit("/", 1)[-1]
        if method == "GET":
            self.gets += 1
            if self.edit_before_patch and memory_id == FACT and self.gets == 3:
                self.rows[FACT]["text"] = self.edit_before_patch
            return dict(self.rows[memory_id])
        if method == "PATCH":
            self.patches.append((memory_id, body))
            self.rows[memory_id].update(state=body["state"], invalidation_reason=body["reason"])
            return {}
        raise AssertionError(method)


@pytest.fixture
def run(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text("cron:\n  model: cron-model\n  model_provider: cron-provider\n")
    sent = []
    monkeypatch.setattr(reviewer, "_connection", lambda home: ("http://bank", ""))
    monkeypatch.setattr(reviewer, "notify", lambda origin, text: sent.append((origin, text)))
    monkeypatch.setitem(sys.modules, "hermes_yaml", types.SimpleNamespace(safe_load=lambda text: {"cron": {"model": "cron-model", "model_provider": "cron-provider"}}))

    def go(bank, llm, user=USER, assistant="", records=None, deliver=None):
        monkeypatch.setattr(reviewer, "_http", bank)
        transcript = "[USER]\n" + user + ("\n\n[ASSISTANT]\n" + assistant if assistant else "")
        job = {"home": str(tmp_path), "session_id": "s", "turn_id": "t", "transcript": transcript, "quotable": [user],
               "recall": {"bank": "b", "records": records or [{"id": OBS, "text": "Staging DB is on 5432."}], "block": "x"},
               "origin": {"platform": "telegram", "chat_id": "1"}, "deliver": deliver}
        return reviewer.review(llm, job), sent
    return go


def test_a_confirmed_contradiction_invalidates_the_source_fact_and_notifies_once(run):
    bank, llm = FakeBank(), FakeLlm()
    applied, sent = run(bank, llm)
    assert applied == 1 and [p[0] for p in bank.patches] == [FACT]
    assert bank.rows[FACT]["state"] == "invalidated" and bank.rows[OBS]["state"] == "valid"
    assert sent == [({"platform": "telegram", "chat_id": "1"}, "🧠 Memory review: 1 invalidated (1 outdated)")]
    assert all(call.get("model") == "cron-model" and call.get("provider") == "cron-provider" for call in llm.calls)


def test_no_write_when_the_second_pass_does_not_confirm(run):
    bank = FakeBank()
    applied, sent = run(bank, FakeLlm(verdict="not_refuted"))
    assert applied == 0 and bank.patches == [] and sent == []


def test_no_write_when_the_quote_is_not_in_the_user_message(run):
    bank = FakeBank()
    applied, sent = run(bank, FakeLlm(quote="the database moved to Dublin"))
    assert applied == 0 and bank.patches == [] and sent == []


def test_a_protected_memory_is_never_invalidated_even_if_proposed(run):
    bank = FakeBank(text="Marcos prefers the staging database on port 5432.")
    applied, sent = run(bank, FakeLlm())
    assert applied == 0 and bank.patches == [] and sent == []


def test_a_fact_edited_before_the_write_is_left_alone(run):
    bank = FakeBank(edit_before_patch="The staging database runs on port 7000.")
    applied, sent = run(bank, FakeLlm())
    assert applied == 0 and bank.patches == [] and sent == []


def test_a_failed_read_back_raises_and_sends_nothing(run, monkeypatch):
    bank = FakeBank()
    original = bank.__call__

    def lying(method, url, key, body=None):
        result = original(method, url, key, body)
        if method == "PATCH":
            bank.rows[FACT]["state"] = "valid"
        return result
    with pytest.raises(RuntimeError):
        run(lying, FakeLlm())


def test_a_quote_only_found_in_assistant_text_is_not_evidence(run):
    bank = FakeBank()
    applied, sent = run(bank, FakeLlm(quote="the staging database is now on 6543"), user="What port is staging on?",
                        assistant="I think the staging database is now on 6543.")
    assert applied == 0 and bank.patches == [] and sent == []


def test_conversation_context_quotes_user_and_tool_text_but_not_memory_blocks():
    pytest.importorskip("agent.background_review")
    rows = [{"role": "user", "content": "check the port <memory-context>The DB is on 5432</memory-context>"},
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "terminal"}}]},
            {"role": "tool", "content": "listening on 6543"},
            {"role": "assistant", "content": "It is 6543."}]
    transcript, quotable = reviewer.conversation_context(rows)
    assert "5432" not in transcript and "[ASSISTANT TOOL CALLS] terminal" in transcript
    assert quotable == ["check the port", "listening on 6543"]


def test_memory_tool_results_are_never_quotable_evidence():
    pytest.importorskip("agent.background_review")
    rows = [{"role": "user", "content": "what port?"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "hindsight_recall"}},
                                                               {"id": "c2", "function": {"name": "terminal"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "1. Staging is on 6543"},
            {"role": "tool", "tool_call_id": "c2", "content": "listening on 6543"}]
    transcript, quotable = reviewer.conversation_context(rows)
    assert quotable == ["what port?", "listening on 6543"]
    assert "Staging is on 6543" in transcript


def test_merge_reviews_injected_and_tool_recalled_memories_once():
    injected = {"bank": "b", "records": [{"id": "1", "text": "a"}], "block": "x"}
    tooled = {"bank": "b", "records": [{"id": "1", "text": "a"}, {"id": "2", "text": "b"}]}
    assert reviewer._merge(injected, tooled)["records"] == [{"id": "1", "text": "a"}, {"id": "2", "text": "b"}]
    assert reviewer._merge(None, tooled)["records"] == tooled["records"]
    assert reviewer._merge(injected, {"bank": "other", "records": []}) is None
    assert reviewer._merge(None, None) is None


def test_batches_of_twenty_run_in_parallel(run):
    bank = FakeBank()
    many = [{"id": f"{i:08d}-0000-0000-0000-000000000000", "text": f"memory {i}"} for i in range(45)]
    for record in many:
        bank.rows[record["id"]] = {"type": "world", "state": "valid", "text": record["text"]}
    import threading
    seen = set()
    llm = FakeLlm(decision="keep")
    original = llm.complete_structured

    def tracking(*a, **k):
        seen.add(threading.current_thread().name)
        __import__("time").sleep(0.05)  # keep calls overlapping so the pool must use several threads
        return original(*a, **k)
    llm.complete_structured = tracking
    applied, sent = run(bank, llm, records=many)
    assert applied == 0 and sum(c["purpose"] == "Hindsight turn review" for c in llm.calls) == 3 and len(seen) >= 2


def test_reimported_module_rewraps_provider_so_its_registry_is_fed(monkeypatch):
    import importlib, sys, types
    class Provider:
        _bank_id, _session_id = "b", "s"
        def prefetch(self, query, *, session_id=""):
            return "block"
        def _tool_recall(self, args):
            return "1. x"
        _TOOL_HANDLERS = {"hindsight_recall": ("query", _tool_recall, "fail")}
    fake = types.SimpleNamespace(import_provider_module=lambda name: types.SimpleNamespace(HindsightMemoryProvider=Provider))
    monkeypatch.setitem(sys.modules, "plugins.memory", fake)
    assert reviewer.patch_provider()
    first = Provider.prefetch
    assert reviewer.patch_provider() and Provider.prefetch is not first
    assert Provider._hindsight_reviewer_originals[0].__name__ == "prefetch" and not getattr(Provider._hindsight_reviewer_originals[0], "_hindsight_reviewer", False)


def test_every_loaded_provider_copy_feeds_one_shared_registry(monkeypatch):
    import sys, types
    def make():
        class Provider:
            _bank_id, _session_id, _recall_sync, _prefetch_method = "b", "s", True, "recall"
            def _build_recall_bank_ids(self):
                return ["b"]
            def _recall(self, *a, **k):
                return [types.SimpleNamespace(id="m1", text="fact")]
            def prefetch(self, query, *, session_id=""):
                self._recall(query)
                return "block"
            def _tool_recall(self, args):
                return "1. x"
            _TOOL_HANDLERS = {"hindsight_recall": ("query", _tool_recall, "fail")}
        return Provider
    live, fresh = make(), make()
    monkeypatch.setitem(sys.modules, "old_hindsight_copy", types.SimpleNamespace(HindsightMemoryProvider=live))
    monkeypatch.setitem(sys.modules, "plugins.memory", types.SimpleNamespace(
        import_provider_module=lambda name: types.SimpleNamespace(HindsightMemoryProvider=fresh)))
    assert reviewer.patch_provider()
    live().prefetch("q", session_id="live-session")
    assert reviewer._recalls.pop("live-session")["records"] == [{"id": "m1", "text": "fact"}]
    assert sys.modules["hermes_hindsight_reviewer_state"].recalls is reviewer._recalls


class NoiseLlm:
    def __init__(self, flags, verdict="noise"):
        self.flags, self.verdict, self.calls = flags, verdict, []

    def complete_structured(self, instructions, input, json_schema, **kwargs):
        self.calls.append(kwargs)
        payload = json.loads(input[0]["text"])
        if kwargs["purpose"] == "Hindsight noise confirmation":
            return types.SimpleNamespace(parsed={"verdict": self.verdict})
        if kwargs["purpose"] == "Hindsight noise review":
            return types.SimpleNamespace(parsed={"items": [
                {"id": c["id"], "decision": "noise" if c["id"] in self.flags else "keep",
                 "reason": self.flags.get(c["id"], ("none", ""))[0], "duplicate_of": self.flags.get(c["id"], ("", ""))[1]}
                for c in payload["MEMORIES"]]})
        return types.SimpleNamespace(parsed={"items": [{"id": c["id"], "decision": "keep", "category": "other", "evidence_quote": ""}
                                                       for c in payload["MEMORIES"]]})


def test_noise_gates_reject_protected_self_and_missing_duplicates():
    cases = [{"id": "a", "memory": "The task is review-only."},
             {"id": "b", "memory": "Marcos prefers short answers."},
             {"id": "c", "memory": "Staging runs on 6543."},
             {"id": "d", "memory": "Staging DB runs on port 6543."}]
    llm = NoiseLlm({"a": ("fragment", ""), "b": ("fragment", ""), "c": ("duplicate", "d"), "d": ("duplicate", "zz")})
    assert policy.noise(llm, cases) == [{"id": "a", "reason": "fragment", "duplicate_of": ""},
                                        {"id": "c", "reason": "duplicate", "duplicate_of": "d"}]


def test_noise_is_invalidated_only_after_confirmation(run):
    bank = FakeBank(text="The task is review-only.")
    applied, sent = run(bank, NoiseLlm({FACT: ("fragment", "")}, verdict="keep"))
    assert applied == 0 and not bank.patches
    applied, sent = run(bank, NoiseLlm({FACT: ("fragment", "")}))
    assert applied == 1 and bank.patches[0][1]["reason"].startswith("hindsight-noise:fragment:")


def test_parallel_model_calls_see_the_callers_context(run):
    import contextvars
    scope = contextvars.ContextVar("scope", default=None)
    seen = []
    llm = FakeLlm(decision="keep")
    original = llm.complete_structured
    def tracking(*a, **k):
        seen.append(scope.get())
        return original(*a, **k)
    llm.complete_structured = tracking
    token = scope.set("gateway-secrets")
    try:
        run(FakeBank(), llm)
    finally:
        scope.reset(token)
    assert seen and set(seen) == {"gateway-secrets"}


def test_a_failing_model_is_called_once_per_task_and_the_batch_is_counted(run):
    bank = FakeBank()
    llm = FakeLlm()
    calls = []
    original = llm.complete_structured
    def rejecting(*a, **k):
        calls.append(k["purpose"])
        if k["purpose"] == "Hindsight noise review":
            raise RuntimeError("Error code: 424")
        return original(*a, **k)
    llm.complete_structured = rejecting
    applied, sent = run(bank, llm)
    assert applied == 1 and len(sent) == 1
    assert calls.count("Hindsight noise review") == 1


def test_no_retry_or_bisect_helpers_remain():
    assert not any(hasattr(reviewer, name) for name in ("with_retries", "with_bisect", "_split", "MAX_ATTEMPTS"))


def test_gateway_handle_survives_a_module_reload():
    assert sys.modules["hermes_hindsight_reviewer_state"].gateway is reviewer._gateway


def test_notice_is_total_then_breakdown_without_text():
    text = reviewer.notice([("duplicate", "a"), ("superseded", "b"), ("duplicate", "c"), ("contradicted", "d"),
                            ("fragment", "token: sk-abcdefghijklmnopqrstuv"), ("duplicate", "e"), ("superseded", "f")])
    assert text == "🧠 Memory review: 7 invalidated (3 duplicate · 2 superseded · 1 no context · 1 outdated)"


def test_notice_uses_the_turns_background_review_sender(run):
    sent_by_review = []
    bank = FakeBank()
    original = reviewer.review
    llm = FakeLlm()
    import types as _t
    job_patch = {"deliver": sent_by_review.append}
    applied, sent = run(bank, llm, deliver=sent_by_review.append)
    assert applied == 1 and not sent and sent_by_review == ["🧠 Memory review: 1 invalidated (1 outdated)"]


def test_a_mid_turn_steering_message_does_not_skip_the_review(monkeypatch):
    pytest.importorskip("agent.memory_context")
    from agent.memory_context import build_memory_context_block
    started = []
    monkeypatch.setattr(reviewer, "_origin", lambda: {"platform": "telegram", "chat_id": "1", "message_id": ""})
    monkeypatch.setattr(reviewer, "_run", started.append)
    monkeypatch.setattr(reviewer.threading, "Thread", lambda target, **k: types.SimpleNamespace(start=target))
    reviewer._recalls["steered"] = {"bank": "b", "records": [{"id": "m1", "text": "fact"}], "block": "- fact"}
    rows = [{"role": "user", "content": "first", "api_content": "first\n\n" + build_memory_context_block("- fact")},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "function": {"name": "terminal"}}]},
            {"role": "tool", "tool_call_id": "c", "content": "ok"},
            {"role": "user", "content": "[OUT-OF-BAND USER MESSAGE]\nalso this\n[/OUT-OF-BAND USER MESSAGE]"}]
    reviewer.on_post_llm_call(session_id="steered", turn_id="t", user_message="first", assistant_response="done",
                              conversation_history=rows)
    assert len(started) == 1 and started[0]["recall"]["records"] == [{"id": "m1", "text": "fact"}]


def test_duplicates_are_found_across_batches(run):
    bank = FakeBank()
    many = [{"id": f"{i:08d}-0000-0000-0000-000000000000", "text": f"memory {i}"} for i in range(25)]
    for record in many:
        bank.rows[record["id"]] = {"type": "world", "state": "valid", "text": record["text"]}
    first, last = many[0]["id"], many[24]["id"]
    seen_other_memories = []

    class CrossLlm(NoiseLlm):
        def complete_structured(self, instructions, input, json_schema, **kwargs):
            payload = json.loads(input[0]["text"])
            if kwargs["purpose"] == "Hindsight noise review":
                others = payload.get("OTHER_MEMORIES", [])
                seen_other_memories.extend(c["memory"] for c in others)
                aliases = {c["memory"]: c["id"] for c in payload["MEMORIES"] + others}
                self.flags = {aliases["memory 24"]: ("duplicate", aliases["memory 0"])}
            return super().complete_structured(instructions, input, json_schema, **kwargs)

    applied, sent = run(bank, CrossLlm({}), records=many)
    assert "memory 0" in seen_other_memories and applied == 1 and bank.rows[last]["state"] == "invalidated"
    assert bank.rows[first]["state"] == "valid"


def test_cross_batch_duplicate_pairs_keep_one_copy():
    rows = [{"id": "a", "reason": "duplicate", "duplicate_of": "b"}, {"id": "b", "reason": "duplicate", "duplicate_of": "a"}]
    assert policy.survivors(rows) == [rows[0]]
    chain = [{"id": "a", "reason": "duplicate", "duplicate_of": "b"}, {"id": "b", "reason": "duplicate", "duplicate_of": "c"},
             {"id": "c", "reason": "fragment", "duplicate_of": ""}]
    assert policy.survivors(chain) == [chain[0], chain[2]]


def test_superseded_needs_a_strictly_newer_target():
    cases = [{"id": "old", "memory": "Curator runs every 168 hours.", "date": "2026-10-01"},
             {"id": "new", "memory": "Curator runs every 24 hours.", "date": "2026-11-09"},
             {"id": "undated", "memory": "Staging uses port 5432.", "date": ""},
             {"id": "pref", "memory": "Marcos prefers weekly reports.", "date": "2026-01-01"}]
    llm = NoiseLlm({"old": ("superseded", "new"), "new": ("superseded", "old"),
                    "undated": ("superseded", "new"), "pref": ("superseded", "new")})
    assert policy.noise(llm, cases) == [{"id": "old", "reason": "superseded", "duplicate_of": "new"}]


def test_superseded_invalidation_is_reported(run):
    bank = FakeBank()
    many = [{"id": f"{i:08d}-0000-0000-0000-000000000000", "text": f"memory {i}"} for i in range(2)]
    bank.rows[many[0]["id"]] = {"type": "world", "state": "valid", "text": "Curator runs every 168 hours.", "mentioned_at": "2026-10-01T00:00:00Z"}
    bank.rows[many[1]["id"]] = {"type": "world", "state": "valid", "text": "Curator runs every 24 hours.", "mentioned_at": "2026-11-09T00:00:00Z"}
    records = [{"id": many[0]["id"], "text": "Curator runs every 168 hours."}, {"id": many[1]["id"], "text": "Curator runs every 24 hours."}]
    applied, sent = run(bank, NoiseLlm({many[0]["id"]: ("superseded", many[1]["id"])}), records=records)
    assert applied == 1 and bank.rows[many[0]["id"]]["state"] == "invalidated" and bank.rows[many[1]["id"]]["state"] == "valid"
    assert sent[0][1] == "🧠 Memory review: 1 invalidated (1 superseded)"


def test_a_provider_loaded_after_registration_is_wrapped(monkeypatch):
    import sys, types
    def make():
        class Provider:
            _bank_id, _session_id, _recall_sync, _prefetch_method = "b", "s", True, "recall"
            def _build_recall_bank_ids(self):
                return ["b"]
            def _recall(self, *a, **k):
                return [types.SimpleNamespace(id="m9", text="late fact")]
            def prefetch(self, query, *, session_id=""):
                self._recall(query)
                return "block"
            def _tool_recall(self, args):
                return "1. x"
            _TOOL_HANDLERS = {"hindsight_recall": ("query", _tool_recall, "fail")}
        return Provider
    first, late = make(), make()
    fake = types.ModuleType("plugins.memory")
    fake._load_package = lambda provider_dir, name: types.SimpleNamespace(HindsightMemoryProvider=late)
    fake.import_provider_module = lambda name: types.SimpleNamespace(HindsightMemoryProvider=first)
    monkeypatch.setitem(sys.modules, "plugins.memory", fake)
    plugins_pkg = sys.modules.get("plugins")
    if plugins_pkg is not None:
        monkeypatch.setattr(plugins_pkg, "memory", fake, raising=False)
    assert reviewer.patch_provider()
    fake._load_package("dir", "hindsight")
    late().prefetch("q", session_id="late-session")
    assert reviewer._recalls.pop("late-session")["records"] == [{"id": "m9", "text": "late fact"}]


def test_notice_sender_only_in_private_chats(monkeypatch):
    import gateway.session_context as sc
    monkeypatch.setattr(sc, 'get_session_env', lambda name, default='': 'telegram' if name == 'HERMES_SESSION_PLATFORM' else default)
    import types
    agent = types.SimpleNamespace(background_review_callback=lambda text: None)
    runner = types.SimpleNamespace(_running_agents={"agent:main:telegram:dm:1": agent, "agent:main:telegram:group:-5": agent})
    monkeypatch.setitem(reviewer._gateway, "runner", runner)
    monkeypatch.setattr(sc, "bound_session_key", lambda: "agent:main:telegram:dm:1")
    assert reviewer._turn_review_callback() is agent.background_review_callback
    monkeypatch.setattr(sc, "bound_session_key", lambda: "agent:main:telegram:group:-5")
    assert reviewer._turn_review_callback() is None


def test_notices_off_keeps_invalidating_but_sends_nothing(run, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_yaml", types.SimpleNamespace(safe_load=lambda text: {
        "cron": {"model": "cron-model", "model_provider": "cron-provider"},
        "plugins": {"entries": {"hermes-agent-guards/hindsight-reviewer": {"notices": False}}}}))
    sent_by_review = []
    applied, sent = run(FakeBank(), FakeLlm(), deliver=sent_by_review.append)
    assert applied == 1 and not sent and sent_by_review == []


def test_notices_default_on_when_setting_absent(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_yaml", types.SimpleNamespace(safe_load=lambda text: {}))
    (tmp_path / "config.yaml").write_text("")
    assert reviewer.notices_enabled(str(tmp_path)) is True


def test_notices_on_when_config_unreadable(tmp_path):
    assert reviewer.notices_enabled(str(tmp_path / "missing")) is True


def _wired_turn(monkeypatch, surface_env, platform):
    started = []
    env = dict(surface_env)
    import gateway.session_context as sc
    monkeypatch.setattr(sc, "get_session_env", lambda name, default="": env.get(name, default))
    monkeypatch.setattr(sc, "bound_session_key", lambda: "")
    monkeypatch.setattr(reviewer, "_origin", lambda: None)
    monkeypatch.setattr(reviewer, "_run", started.append)
    monkeypatch.setattr(reviewer.threading, "Thread", lambda target, **k: types.SimpleNamespace(start=target))
    reviewer._recalls["turn"] = {"bank": "b", "records": [{"id": "m1", "text": "fact"}], "block": "- fact"}
    return started


def _history():
    from agent.memory_context import build_memory_context_block
    return [{"role": "user", "content": "q", "api_content": "q\n\n" + build_memory_context_block("- fact")}]


@pytest.mark.parametrize("platform", ["cli", "tui", "gui", "desktop", ""])
def test_a_turn_without_a_live_viewer_is_reviewed_silently(monkeypatch, platform):
    pytest.importorskip("agent.memory_context")
    started = _wired_turn(monkeypatch, {}, platform)
    reviewer.on_post_llm_call(session_id="turn", turn_id="t", user_message="q", assistant_response="a",
                              conversation_history=_history(), platform=platform)
    assert len(started) == 1
    job = started[0]
    assert job["attended"] is True and job["surface"] == platform
    assert job["deliver"] is None and job["defer_notice"] is True and job["origin"]["message_id"] == ""


@pytest.mark.parametrize("platform,env", [
    ("cron", {}), ("subagent", {}), ("curator", {}), ("kanban", {}), ("oneshot", {}),
    ("cli", {"HERMES_CRON_SESSION": "1"}), ("gui", {"HERMES_DELEGATED_CHILD_CONTEXT": "true"}),
    ("cli", {"HERMES_SINGLE_QUERY_SESSION": "1"}), ("cli", {"HERMES_KANBAN_TASK": "t_1"})])
def test_machine_written_turns_are_reviewed_without_contradiction_proposals(monkeypatch, platform, env):
    pytest.importorskip("agent.memory_context")
    started = _wired_turn(monkeypatch, env, platform)
    reviewer.on_post_llm_call(session_id="turn", turn_id="t", user_message="q", assistant_response="a",
                              conversation_history=_history(), platform=platform)
    assert len(started) == 1 and started[0]["attended"] is False


def test_an_unattended_review_never_asks_the_model_for_contradictions(monkeypatch, tmp_path):
    monkeypatch.setattr(reviewer, "_connection", lambda home: ("http://bank", ""))
    monkeypatch.setitem(sys.modules, "hermes_yaml", types.SimpleNamespace(safe_load=lambda text: {}))
    (tmp_path / "config.yaml").write_text("")
    bank, llm = FakeBank(), FakeLlm()
    monkeypatch.setattr(reviewer, "_http", bank)
    job = {"home": str(tmp_path), "session_id": "s", "turn_id": "t", "transcript": "[USER]\n" + USER, "quotable": [USER],
           "recall": {"bank": "b", "records": [{"id": OBS, "text": "Staging DB is on 5432."}], "block": "x"},
           "origin": {"platform": "cron", "chat_id": "s"}, "deliver": None, "attended": False}
    assert reviewer.review(llm, job) == 0
    assert bank.patches == []
    purposes = {call["purpose"] for call in llm.calls}
    assert "Hindsight turn review" not in purposes and "Hindsight turn review confirmation" not in purposes
    assert "Hindsight noise review" in purposes


def test_unattended_reviews_share_two_slots():
    assert reviewer._unattended_slots._value == 2
    assert reviewer.UNATTENDED_SURFACES >= {"cron", "subagent", "curator", "kanban"}


def _audit_events(caplog, event):
    rows = [json.loads(r.getMessage().split("audit: ", 1)[1]) for r in caplog.records if "Hindsight review audit" in r.getMessage()]
    return [row for row in rows if row.get("event") == event]


class StatusError(Exception):
    def __init__(self, status_code, text="boom"):
        super().__init__(text)
        self.status_code = status_code


@pytest.mark.parametrize("error, reason", [
    (StatusError(404, "model SECRET-TEXT not found"), "http_404"),
    (StatusError(424), "http_424"),
    (urllib.error.HTTPError("http://bank", 503, "x", {}, None), "http_503"),
    (ValueError("Incomplete model output: truncated"), "truncated"),
    (ValueError("Incomplete model output"), "incomplete_output"),
    (ValueError("Duplicate case ids"), "duplicate_case_ids"),
    (ValueError("Plugin LLM structured output did not match schema: 'x' is required"), "schema_mismatch"),
    (ValueError("anything else"), "value_error"),
    (TimeoutError("slow"), "timeout"),
    (ConnectionError("down"), "connection"),
    (RuntimeError("secret memory text"), "other"),
])
def test_failure_reason_is_a_fixed_token_never_the_exception_text(error, reason):
    assert reviewer.failure_reason(error) == reason
    assert "SECRET" not in reviewer.failure_reason(error) and "secret" not in reviewer.failure_reason(error)


def _structured(parsed, output_tokens=0):
    return types.SimpleNamespace(parsed=parsed, usage=types.SimpleNamespace(output_tokens=output_tokens))


def test_an_unparsed_reply_near_the_token_budget_is_reported_as_truncated():
    case = [{"id": "a", "memory": "m", "date": ""}]
    llm = types.SimpleNamespace(complete_structured=lambda **k: _structured(None, output_tokens=k["max_tokens"]))
    with pytest.raises(ValueError, match="truncated"):
        policy.noise(llm, case)
    with pytest.raises(ValueError, match="truncated"):
        policy.propose(llm, case)


def test_an_unparsed_reply_well_under_the_budget_is_not_called_truncated():
    case = [{"id": "a", "memory": "m", "date": "", "evidence": ""}]
    llm = types.SimpleNamespace(complete_structured=lambda **k: _structured(None, output_tokens=10))
    with pytest.raises(ValueError) as caught:
        policy.noise(llm, case)
    assert reviewer.failure_reason(caught.value) == "incomplete_output"


def test_noise_lane_budget_covers_a_full_batch_of_uuid_rows():
    seen = []
    llm = types.SimpleNamespace(complete_structured=lambda **k: seen.append(k["max_tokens"]) or _structured({"items": []}))
    policy.noise(llm, [{"id": "%036d" % n, "memory": "m", "date": ""} for n in range(20)])
    assert seen[0] >= 20 * 100


def test_gaps_split_lost_coverage_from_deliberate_abstentions():
    lost, abstained = reviewer._gaps(Counter({"noise_failed_cases": 20, "review_missing_rows": 2, "confirmation_errors": 1,
                                              "noise_rejected_rows": 41, "unquotable": 3, "stale_records": 1,
                                              "noise_deferred": 2, "applied": 4}))
    assert (lost, abstained) == (23, 47)
    assert reviewer._gaps(Counter({"applied": 3, "noise_decided": 9})) == (0, 0)


def test_a_failed_batch_is_partial_with_a_reason_and_no_memory_text(run, caplog, monkeypatch):
    caplog.set_level("INFO")
    monkeypatch.setattr(policy, "complete_structured", lambda llm, **kw: llm.complete_structured(**kw))
    llm = FakeLlm()
    original = llm.complete_structured
    def failing(*a, **k):
        if k["purpose"] == "Hindsight noise review":
            raise StatusError(404, "model not found for " + USER)
        return original(*a, **k)
    llm.complete_structured = failing
    run(FakeBank(), llm)
    failed = _audit_events(caplog, "batch_failed")
    assert failed and failed[0]["reason"] == "http_404" and failed[0]["lane"] == "noise" and failed[0]["size"] == 1
    finished = _audit_events(caplog, "finished")[0]
    assert finished["status"] == "partial" and finished["lost"] >= 1
    assert finished["failure_reasons"] == {"noise:http_404": 1}
    assert USER not in caplog.text and "model not found" not in "".join(r.getMessage() for r in caplog.records if "audit" in r.getMessage())


def test_abstentions_alone_keep_the_review_complete(run, caplog):
    caplog.set_level("INFO")
    llm = FakeLlm(decision="keep")
    original = llm.complete_structured
    def invalid_target(*a, **k):
        if k["purpose"] == "Hindsight noise review":
            payload = json.loads(k["input"][0]["text"] if "input" in k else a[1][0]["text"])
            rows = [{"id": c["id"], "decision": "noise", "reason": "duplicate", "duplicate_of": "not-a-case"} for c in payload["MEMORIES"]]
            return types.SimpleNamespace(parsed={"items": rows})
        return original(*a, **k)
    llm.complete_structured = invalid_target
    run(FakeBank(), llm)
    finished = _audit_events(caplog, "finished")[0]
    assert finished["status"] == "complete" and finished["lost"] == 0 and finished["abstained"] >= 1


def test_noise_confirmer_prompt_pins_the_calibrated_per_reason_tests():
    text = policy.NOISE_CONFIRM_INSTRUCTIONS
    assert 'answer "keep" only if you can name the specific fact that would be lost' in text
    assert 'every concrete fact in MEMORY' in text and 'extra detail in KEPT is fine' in text
    assert 'AND holds no specific name, value, number, path, identifier or decision' in text
    assert 'Always answer "keep" for a preference, authorization, commitment or identity fact' in text
    assert 'different scopes, environments or periods mean "keep"' in text


def test_noise_confirmer_still_vetoes_on_an_unparsed_verdict():
    llm = types.SimpleNamespace(complete_structured=lambda **k: types.SimpleNamespace(parsed={"verdict": "maybe"}))
    assert policy.confirm_noise(llm, "m", "duplicate", "k") is False
    llm = types.SimpleNamespace(complete_structured=lambda **k: types.SimpleNamespace(parsed=None))
    assert policy.confirm_noise(llm, "m", "duplicate", "k") is False


KEEPER = "33333333-3333-3333-3333-333333333333"


def _duplicate_bank(edit_keeper=None):
    class Bank(FakeBank):
        keeper_reads = 0

        def __call__(self, method, url, key, body=None):
            if method == "GET" and url.rsplit("/", 1)[-1] == KEEPER:
                self.keeper_reads += 1
                if edit_keeper and self.keeper_reads == 2:
                    self.rows[KEEPER]["text"] = edit_keeper
            return super().__call__(method, url, key, body)
    bank = Bank(text="Staging DB runs on port 6543.")
    bank.rows[FACT]["mentioned_at"] = "2026-10-01T00:00:00Z"
    bank.rows[KEEPER] = {"type": "world", "state": "valid", "text": "The staging database listens on port 6543, host stg-db.",
                         "mentioned_at": "2026-11-09T00:00:00Z"}
    return bank


def _records():
    return [{"id": FACT, "text": "Staging DB runs on port 6543."}, {"id": KEEPER, "text": "The staging database listens on port 6543, host stg-db."}]


def test_a_duplicate_flag_is_invalidated_without_asking_the_second_checker(run):
    bank = _duplicate_bank()
    llm = NoiseLlm({FACT: ("duplicate", KEEPER)}, verdict="keep")
    applied, sent = run(bank, llm, records=_records())
    assert applied == 1 and bank.rows[FACT]["state"] == "invalidated" and bank.rows[KEEPER]["state"] == "valid"
    assert bank.patches[0][1]["reason"].startswith("hindsight-noise:duplicate:")
    assert "Hindsight noise confirmation" not in [call["purpose"] for call in llm.calls]


@pytest.mark.parametrize("reason, target", [("fragment", ""), ("transient", ""), ("superseded", KEEPER)])
def test_every_other_reason_still_needs_the_second_checker(run, reason, target):
    bank = _duplicate_bank()
    llm = NoiseLlm({FACT: (reason, target)}, verdict="keep")
    applied, sent = run(bank, llm, records=_records())
    assert applied == 0 and not bank.patches
    assert "Hindsight noise confirmation" in [call["purpose"] for call in llm.calls]


def test_a_duplicate_whose_kept_copy_changed_before_the_write_is_left_alone(run):
    bank = _duplicate_bank(edit_keeper="Something else entirely about the staging database.")
    llm = NoiseLlm({FACT: ("duplicate", KEEPER)}, verdict="keep")
    applied, sent = run(bank, llm, records=_records())
    assert applied == 0 and bank.rows[FACT]["state"] == "valid"


def test_a_duplicate_of_a_protected_memory_is_never_written(run):
    bank = _duplicate_bank()
    bank.rows[FACT]["text"] = "Marcos prefers the staging database on port 6543."
    llm = NoiseLlm({FACT: ("duplicate", KEEPER)}, verdict="keep")
    applied, sent = run(bank, llm, records=[{"id": FACT, "text": "Marcos prefers the staging database on port 6543."}, _records()[1]])
    assert applied == 0 and bank.rows[FACT]["state"] == "valid"


def test_skipped_confirmations_are_audited_and_counted_without_memory_text(run, caplog):
    caplog.set_level("INFO")
    run(_duplicate_bank(), NoiseLlm({FACT: ("duplicate", KEEPER)}, verdict="keep"), records=_records())
    skipped = _audit_events(caplog, "confirmation_skipped")
    assert skipped and skipped[0]["lane"] == "noise" and skipped[0]["reason"] == "duplicate_trusted" and skipped[0]["kept_id"] == KEEPER
    finished = _audit_events(caplog, "finished")[0]
    assert finished["counts"]["confirmation_skipped"] == 1 and finished["status"] == "complete"
    assert "staging database" not in caplog.text
