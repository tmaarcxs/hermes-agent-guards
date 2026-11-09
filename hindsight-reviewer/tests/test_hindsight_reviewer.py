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

    def go(bank, llm, user=USER, assistant=""):
        monkeypatch.setattr(reviewer, "_http", bank)
        transcript = "[USER]\n" + user + ("\n\n[ASSISTANT]\n" + assistant if assistant else "")
        job = {"home": str(tmp_path), "session_id": "s", "turn_id": "t", "transcript": transcript, "quotable": [user],
               "recall": {"bank": "b", "records": [{"id": OBS, "text": "Staging DB is on 5432."}], "block": "x"},
               "origin": {"platform": "telegram", "chat_id": "1"}}
        return reviewer.review(llm, job), sent
    return go


def test_a_confirmed_contradiction_invalidates_the_source_fact_and_notifies_once(run):
    bank, llm = FakeBank(), FakeLlm()
    applied, sent = run(bank, llm)
    assert applied == 1 and [p[0] for p in bank.patches] == [FACT]
    assert bank.rows[FACT]["state"] == "invalidated" and bank.rows[OBS]["state"] == "valid"
    assert sent == [({"platform": "telegram", "chat_id": "1"}, reviewer.NOTICE_ONE)]
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
