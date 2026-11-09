"""The Jot reviewer writes only gated, quoted, confirmed board changes and notifies once."""

import datetime
import importlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parents[1]
if "jot_reviewer" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("jot_reviewer", _root / "__init__.py", submodule_search_locations=[str(_root)])
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["jot_reviewer"] = _module
    _spec.loader.exec_module(_module)
reviewer = importlib.import_module("jot_reviewer.reviewer")
policy = reviewer.policy

NOTE = """> 🧠 Tablero semanal de trabajo.

## 📅 Lunes 9

| Estado | Tarea | Siguiente paso |
| --- | --- | --- |
| ⏳ | AuditQC: alta en Application Insights | Esperar respuesta de TRANSREQ-902 |
| 🔄 | Consulta: por qué falla el job de PRE | Revisar los logs |

## 📅 Martes 10

| Estado | Tarea | Siguiente paso |
| --- | --- | --- |
| ⚪ |  |  |

## 📅 Miércoles 11

| Estado | Tarea | Siguiente paso |
| --- | --- | --- |
| ⚪ |  |  |

## 📅 Jueves 12

| Estado | Tarea | Siguiente paso |
| --- | --- | --- |
| ⚪ |  |  |

## 📅 Viernes 13

| Estado | Tarea | Siguiente paso |
| --- | --- | --- |
| ⚪ |  |  |
"""
CONTEXT = ("[USER]\nya han contestado TRANSREQ-902, estamos dados de alta\n\n[ASSISTANT]\nPR #1460 mergeada y desplegada en DES.\n\n"
           "[USER]\nlo del job de PRE ya está, era la variable")


def test_parse_note_reads_days_in_order_and_skips_placeholders():
    days = policy.parse_note(NOTE)
    assert list(days) == ["Lunes 9", "Martes 10", "Miércoles 11", "Jueves 12", "Viernes 13"]
    assert days["Lunes 9"][0] == {"state": "⏳", "task": "AuditQC: alta en Application Insights", "next": "Esperar respuesta de TRANSREQ-902"}
    assert days["Martes 10"] == []


def _change(op, day, task, state="🔄", nxt="Revisar", quote="PR #1460 mergeada"):
    return {"op": op, "day": day, "task": task, "state": state, "next": nxt, "evidence_quote": quote}


def test_gate_enforces_board_rules():
    days = policy.parse_note(NOTE)
    ok = [
        _change("set", "Lunes 9", "AuditQC: alta en Application Insights", "✅", "Nada", "estamos dados de alta"),
        _change("add", "Martes 10", "AuditQC: PR #1460 en DES", "✅", "nada", "PR #1460 mergeada y desplegada en DES"),
        _change("remove", "Lunes 9", "Consulta: por qué falla el job de PRE", "✅", "Nada", "lo del job de PRE ya está"),
    ]
    bad = [
        _change("add", "Lunes 9", "AuditQC: algo de ayer"),                       # adds only go to today
        _change("add", "Martes 10", "AuditQC: alta en Application Insights"),     # duplicate of a live row
        _change("add", "Martes 10", "Hermes: worker card t_1234abcd"),            # forbidden words / card ids
        _change("add", "Martes 10", "AuditQC: x", "✅", "Revisar"),                # done must be Nada
        _change("add", "Martes 10", "AuditQC: y", quote="not in the context"),     # quote must exist
        _change("set", "Lunes 9", "No existe"),                                   # row must exist
        _change("remove", "Lunes 9", "AuditQC: alta en Application Insights"),    # only closed 🔄 one-offs vanish
        _change("add", "Domingo 15", "AuditQC: z"),                               # unknown day
    ]
    kept = policy.gate(ok, days, "Martes 10", CONTEXT)
    assert [(c["op"], c["task"]) for c in kept] == [(c["op"], c["task"]) for c in ok]
    assert kept[1]["next"] == "Nada"
    for change in bad:
        assert policy.gate([change], days, "Martes 10", CONTEXT) == [], change


def test_gate_allows_moving_an_open_row_to_today():
    days = policy.parse_note(NOTE)
    move = [_change("remove", "Lunes 9", "AuditQC: alta en Application Insights", "⏳", "x", "estamos dados de alta"),
            _change("add", "Martes 10", "AuditQC: alta en Application Insights", "⏳", "Confirmar acceso", "estamos dados de alta")]
    assert len(policy.gate(move, days, "Martes 10", CONTEXT)) == 2


def test_today_heading_and_weekend():
    assert reviewer.today_heading(datetime.date(2026, 11, 10)) == "Martes 10"
    assert reviewer.today_heading(datetime.date(2026, 11, 14)) is None


def test_notice_is_total_then_breakdown():
    assert reviewer.notice([{"op": "set"}, {"op": "add"}, {"op": "set"}]) == "📝 Jot review: 3 changes (2 updated · 1 added)"
    assert reviewer.notice([{"op": "remove"}]) == "📝 Jot review: 1 change (1 removed)"


class FakeLlm:
    def __init__(self, changes, verdict="apply"):
        self.changes, self.verdict, self.calls = changes, verdict, []

    def complete_structured(self, instructions, input, json_schema, **kwargs):
        self.calls.append(kwargs)
        if kwargs["purpose"] == "Jot turn review confirmation":
            return types.SimpleNamespace(parsed={"verdict": self.verdict})
        return types.SimpleNamespace(parsed={"changes": self.changes})


@pytest.fixture
def wired(monkeypatch, tmp_path):
    writes = []
    monkeypatch.setattr(reviewer, "cron_route", lambda home: {"provider": "p", "model": "m"})
    monkeypatch.setattr(reviewer, "week_note", lambda today: ({"id": "n1", "markdown": NOTE}, policy.parse_note(NOTE)))
    monkeypatch.setattr(reviewer, "_jot", lambda name, args: {"markdown": NOTE})
    monkeypatch.setattr(reviewer, "_script", lambda home, name, *args: writes.append((name, args)) or True)
    sent = []
    job = {"home": str(tmp_path), "transcript": CONTEXT, "deliver": sent.append}
    return job, writes, sent


def test_review_applies_confirmed_changes_and_notifies(wired):
    job, writes, sent = wired
    llm = FakeLlm([_change("set", "Lunes 9", "AuditQC: alta en Application Insights", "✅", "Nada", "estamos dados de alta")])
    applied = reviewer.review(llm, job, now=datetime.date(2026, 11, 10))
    assert len(applied) == 1 and writes[0][0] == "set-rich-row"
    assert writes[0][1] == ("n1", "Lunes 9", "AuditQC: alta en Application Insights", "⏳",
                            "Esperar respuesta de TRANSREQ-902", "✅", "Nada", "--write")
    assert sent == ["📝 Jot review: 1 change (1 updated)"]
    assert all(call["model"] == "m" for call in llm.calls)


def test_review_writes_nothing_when_the_checker_says_skip(wired):
    job, writes, sent = wired
    llm = FakeLlm([_change("add", "Martes 10", "AuditQC: PR #1460 en DES", "✅", "Nada", "PR #1460 mergeada")], verdict="skip")
    assert reviewer.review(llm, job, now=datetime.date(2026, 11, 10)) == [] and not writes and not sent


def test_review_removes_before_adding_a_moved_row(wired):
    job, writes, sent = wired
    llm = FakeLlm([_change("add", "Martes 10", "AuditQC: alta en Application Insights", "⏳", "Confirmar acceso", "estamos dados de alta"),
                   _change("remove", "Lunes 9", "AuditQC: alta en Application Insights", "⏳", "x", "estamos dados de alta")])
    reviewer.review(llm, job, now=datetime.date(2026, 11, 10))
    assert [name for name, _ in writes] == ["remove-row", "append-row"]


def test_model_calls_retry_up_to_ten_times(monkeypatch):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 10:
            raise RuntimeError("424")
        return "ok"
    assert reviewer.with_retries(flaky, sleep=lambda s: None) == "ok" and len(calls) == 10


def test_scripts_take_paths_from_the_environment():
    for name in ("append-row", "remove-row", "set-rich-row"):
        text = (_root / "scripts" / f"{name}.mts").read_text()
        assert "/Users/" not in text and "process.env.JOT_PLUGIN_DIR" in text and "process.env.JOT_DATA_DIR" in text


def test_notice_sender_only_in_private_chats(monkeypatch):
    import gateway.session_context as sc
    import types
    agent = types.SimpleNamespace(background_review_callback=lambda text: None)
    runner = types.SimpleNamespace(_running_agents={"agent:main:telegram:dm:1": agent, "agent:main:telegram:group:-5": agent})
    monkeypatch.setitem(reviewer._gateway, "runner", runner)
    monkeypatch.setattr(sc, "bound_session_key", lambda: "agent:main:telegram:dm:1")
    assert reviewer._turn_review_callback() is agent.background_review_callback
    monkeypatch.setattr(sc, "bound_session_key", lambda: "agent:main:telegram:group:-5")
    assert reviewer._turn_review_callback() is None
