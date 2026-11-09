"""The reviewer invalidates only a confirmed, quoted, unchanged primary fact and notifies once."""

import importlib
import importlib.util
import json
import sys
import types
import urllib.error
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
    assert sent == [({"platform": "telegram", "chat_id": "1"}, "🧠 Memory review: invalidated 'The staging database runs on port 5432.' (outdated)")]
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


def test_model_calls_are_retried_up_to_ten_attempts():
    calls = []

    def flaky(value):
        calls.append(value)
        if len(calls) < 10:
            raise RuntimeError("upstream 424")
        return value
    assert reviewer.with_retries(flaky, "ok", sleep=lambda s: None) == "ok" and len(calls) == 10
    calls.clear()

    def broken(value):
        calls.append(value)
        raise RuntimeError("down")
    with pytest.raises(RuntimeError):
        reviewer.with_retries(broken, "x", sleep=lambda s: None)
    assert len(calls) == 10


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


def test_a_rejected_batch_skips_only_its_memories(run, monkeypatch):
    monkeypatch.setattr(reviewer, "MAX_ATTEMPTS", 2)
    monkeypatch.setattr(reviewer.time if hasattr(reviewer, "time") else __import__("time"), "sleep", lambda s: None)
    bank = FakeBank()
    llm = FakeLlm()
    original = llm.complete_structured
    def rejecting(*a, **k):
        if k["purpose"] == "Hindsight noise review":
            raise RuntimeError("Error code: 424")
        return original(*a, **k)
    llm.complete_structured = rejecting
    applied, sent = run(bank, llm)
    assert applied == 1 and len(sent) == 1


def test_bisect_isolates_the_memory_the_provider_rejects(monkeypatch):
    monkeypatch.setattr(reviewer, "MAX_ATTEMPTS", 1)
    def call(llm, batch):
        if any(case["id"] == "bad" for case in batch):
            raise RuntimeError("424")
        return [case["id"] for case in batch]
    call.__name__ = "noise"
    batch = [{"id": str(i)} for i in range(7)] + [{"id": "bad"}] + [{"id": "8"}]
    assert reviewer.with_bisect(call, None, batch) == ["0", "1", "2", "3", "4", "5", "6", "8"]


def test_gateway_handle_survives_a_module_reload():
    assert sys.modules["hermes_hindsight_reviewer_state"].gateway is reviewer._gateway


def test_notice_is_short_and_masks_secrets():
    text = reviewer.notice([("contradicted", "Kanban is the execution layer."), ("duplicate", "x " * 60),
                            ("fragment", "token: sk-abcdefghijklmnopqrstuv"), ("transient", "a"), ("transient", "b")])
    assert text == ("🧠 Memory review: invalidated 'Kanban is the execution layer.' (outdated) · "
                    "'" + ("x " * 20).strip()[:39] + "…' (duplicate) · '[withheld]' (no context) · +2 more")


def test_notice_uses_the_turns_background_review_sender(run):
    sent_by_review = []
    bank = FakeBank()
    original = reviewer.review
    llm = FakeLlm()
    import types as _t
    job_patch = {"deliver": sent_by_review.append}
    applied, sent = run(bank, llm, deliver=sent_by_review.append)
    assert applied == 1 and not sent and sent_by_review == ["🧠 Memory review: invalidated 'The staging database runs on port 5432.' (outdated)"]


def test_a_mid_turn_steering_message_does_not_skip_the_review(monkeypatch):
    pytest.importorskip("agent.memory_context")
    from agent.memory_context import build_memory_context_block
    started = []
    monkeypatch.setattr(reviewer, "_origin", lambda: {"platform": "telegram", "chat_id": "1"})
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
    seen_other_ids = []

    class CrossLlm(NoiseLlm):
        def complete_structured(self, instructions, input, json_schema, **kwargs):
            payload = json.loads(input[0]["text"])
            if kwargs["purpose"] == "Hindsight noise review":
                seen_other_ids.extend(c["id"] for c in payload.get("OTHER_MEMORIES", []))
            return super().complete_structured(instructions, input, json_schema, **kwargs)

    applied, sent = run(bank, CrossLlm({last: ("duplicate", first)}), records=many)
    assert first in seen_other_ids and applied == 1 and bank.rows[last]["state"] == "invalidated"
    assert bank.rows[first]["state"] == "valid"


def test_cross_batch_duplicate_pairs_keep_one_copy():
    rows = [{"id": "a", "reason": "duplicate", "duplicate_of": "b"}, {"id": "b", "reason": "duplicate", "duplicate_of": "a"}]
    assert policy.survivors(rows) == [rows[0]]
    chain = [{"id": "a", "reason": "duplicate", "duplicate_of": "b"}, {"id": "b", "reason": "duplicate", "duplicate_of": "c"},
             {"id": "c", "reason": "fragment", "duplicate_of": ""}]
    assert policy.survivors(chain) == [chain[0], chain[2]]
