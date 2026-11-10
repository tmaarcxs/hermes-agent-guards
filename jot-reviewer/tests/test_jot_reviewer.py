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
| ⏳ | Servicio: alta en Application Insights | Esperar respuesta de TICKET-902 |
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
CONTEXT = ("[USER]\nya han contestado TICKET-902, estamos dados de alta\n\n[ASSISTANT]\nPR #1460 mergeada y desplegada en DES.\n\n"
           "[USER]\nlo del job de PRE ya está, era la variable")


def test_parse_note_reads_days_in_order_and_skips_placeholders():
    days = policy.parse_note(NOTE)
    assert list(days) == ["Lunes 9", "Martes 10", "Miércoles 11", "Jueves 12", "Viernes 13"]
    assert days["Lunes 9"][0] == {"state": "⏳", "task": "Servicio: alta en Application Insights", "next": "Esperar respuesta de TICKET-902"}
    assert days["Martes 10"] == []


def _change(op, day, task, state="🔄", nxt="Revisar", quote="PR #1460 mergeada"):
    return {"op": op, "day": day, "task": task, "state": state, "next": nxt, "evidence_quote": quote}


def test_gate_enforces_board_rules():
    days = policy.parse_note(NOTE)
    ok = [
        _change("set", "Lunes 9", "Servicio: alta en Application Insights", "✅", "Nada", "estamos dados de alta"),
        _change("add", "Martes 10", "Servicio: PR #1460 en DES", "✅", "nada", "PR #1460 mergeada y desplegada en DES"),
        _change("remove", "Lunes 9", "Consulta: por qué falla el job de PRE", "✅", "Nada", "lo del job de PRE ya está"),
    ]
    bad = [
        _change("add", "Lunes 9", "Servicio: algo de ayer"),                       # adds only go to today
        _change("add", "Martes 10", "Servicio: alta en Application Insights"),     # duplicate of a live row
        _change("add", "Martes 10", "Hermes: worker card t_1234abcd"),            # forbidden words / card ids
        _change("add", "Martes 10", "Servicio: x", "✅", "Revisar"),                # done must be Nada
        _change("add", "Martes 10", "Servicio: y", quote="not in the context"),     # quote must exist
        _change("set", "Lunes 9", "No existe"),                                   # row must exist
        _change("remove", "Lunes 9", "Servicio: alta en Application Insights"),    # only closed 🔄 one-offs vanish
        _change("add", "Domingo 15", "Servicio: z"),                               # unknown day
    ]
    kept = policy.gate(ok, days, "Martes 10", CONTEXT)
    assert [(c["op"], c["task"]) for c in kept] == [(c["op"], c["task"]) for c in ok]
    assert kept[1]["next"] == "Nada"
    for change in bad:
        assert policy.gate([change], days, "Martes 10", CONTEXT) == [], change


def test_gate_allows_moving_an_open_row_to_today():
    days = policy.parse_note(NOTE)
    move = [_change("remove", "Lunes 9", "Servicio: alta en Application Insights", "⏳", "x", "estamos dados de alta"),
            _change("add", "Martes 10", "Servicio: alta en Application Insights", "⏳", "Confirmar acceso", "estamos dados de alta")]
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
    llm = FakeLlm([_change("set", "Lunes 9", "Servicio: alta en Application Insights", "✅", "Nada", "estamos dados de alta")])
    applied = reviewer.review(llm, job, now=datetime.date(2026, 11, 10))
    assert len(applied) == 1 and writes[0][0] == "set-rich-row"
    assert writes[0][1] == ("n1", "Lunes 9", "Servicio: alta en Application Insights", "⏳",
                            "Esperar respuesta de TICKET-902", "✅", "Nada", "--write")
    assert sent == ["📝 Jot review: 1 change (1 updated)"]
    assert all(call["model"] == "m" for call in llm.calls)


def test_review_writes_nothing_when_the_checker_says_skip(wired):
    job, writes, sent = wired
    llm = FakeLlm([_change("add", "Martes 10", "Servicio: PR #1460 en DES", "✅", "Nada", "PR #1460 mergeada")], verdict="skip")
    assert reviewer.review(llm, job, now=datetime.date(2026, 11, 10)) == [] and not writes and not sent


def test_review_removes_before_adding_a_moved_row(wired):
    job, writes, sent = wired
    llm = FakeLlm([_change("add", "Martes 10", "Servicio: alta en Application Insights", "⏳", "Confirmar acceso", "estamos dados de alta"),
                   _change("remove", "Lunes 9", "Servicio: alta en Application Insights", "⏳", "x", "estamos dados de alta")])
    reviewer.review(llm, job, now=datetime.date(2026, 11, 10))
    assert [name for name, _ in writes] == ["remove-row", "append-row"]


def test_a_failing_model_call_is_not_retried():
    assert not any(hasattr(reviewer, name) for name in ("with_retries", "MAX_ATTEMPTS"))
    calls = []

    class Llm:
        def complete_structured(self, **kwargs):
            calls.append(1)
            raise RuntimeError("424")
    with pytest.raises(RuntimeError):
        reviewer.policy.propose(Llm(), "md", "Lunes 9", "turn", {})
    assert len(calls) == 1


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


def test_evidence_is_only_the_current_turn():
    rows = [{"role": "user", "content": "mergea la #1460"}, {"role": "assistant", "content": "PR #1460 mergeada"},
            {"role": "user", "content": "¿qué hora es?"}, {"role": "assistant", "content": "las diez"}]
    earlier, turn = reviewer.split_turn(rows)
    text = reviewer.with_earlier(earlier, turn)
    assert "PR #1460 mergeada" in earlier and "PR #1460" not in turn and "las diez" in turn
    assert text.index("EARLIER TURNS") < text.index("THIS TURN")
    assert reviewer.with_earlier("", turn) == turn


def test_a_quote_from_earlier_turns_is_not_evidence(wired):
    job, writes, sent = wired
    job.update(turn="[USER]\nlo del job de PRE ya está", transcript="=== EARLIER TURNS ===\nPR #1460 mergeada\n=== THIS TURN ===\n[USER]\nlo del job de PRE ya está")
    llm = FakeLlm([_change("add", "Martes 10", "Servicio: PR #1460 en DES", "✅", "Nada", "PR #1460 mergeada")])
    assert reviewer.review(llm, job, now=datetime.date(2026, 11, 10)) == [] and not writes


def test_review_rereads_the_note_before_writing_and_skips_when_it_changed(wired, monkeypatch):
    job, writes, sent = wired
    taken = NOTE.replace("| ⚪ |  |  |\n\n## 📅 Miércoles", "| 🔄 | Servicio: PR #1460 en DES | Revisar |\n\n## 📅 Miércoles", 1)
    monkeypatch.setattr(reviewer, "_jot", lambda name, args: {"markdown": taken})
    llm = FakeLlm([_change("add", "Martes 10", "Servicio: PR #1460 en DES (repo#1460)", "✅", "Nada", "PR #1460 mergeada")])
    assert reviewer.review(llm, job, now=datetime.date(2026, 11, 10)) == [] and not writes


def test_reviews_run_one_at_a_time(wired):
    import threading
    job, writes, sent = wired
    inside, overlap = [], []

    class Slow(FakeLlm):
        def complete_structured(self, *args, **kwargs):
            inside.append(1)
            overlap.append(len(inside))
            import time
            time.sleep(0.02)
            inside.pop()
            return super().complete_structured(*args, **kwargs)
    threads = [threading.Thread(target=reviewer.review, args=(Slow([]), job, datetime.date(2026, 11, 10))) for _ in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert max(overlap) == 1


@pytest.mark.parametrize("name", ["HERMES_CRON_SESSION", "HERMES_KANBAN_TASK", "HERMES_DELEGATED_CHILD_CONTEXT"])
def test_unattended_turns_never_start_a_review(monkeypatch, name):
    started = []
    monkeypatch.setattr(reviewer.threading, "Thread", lambda **kw: started.append(kw))
    monkeypatch.setattr(reviewer, "_origin", lambda: {"platform": "telegram", "chat_id": "1"})
    monkeypatch.setattr(reviewer, "_jot_available", lambda: True)
    reviewer.on_post_llm_call(user_message="hola", assistant_response="ok", conversation_history=[])
    assert started
    started.clear()
    monkeypatch.setenv(name, "1")
    reviewer.on_post_llm_call(user_message="hola", assistant_response="ok", conversation_history=[])
    assert not started


def _db(tmp_path, rows):
    import sqlite3
    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)")
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
    for i, (source, role, content, stamp) in enumerate(rows):
        db.execute("INSERT OR IGNORE INTO sessions VALUES (?, ?)", (f"s-{source}", source))
        db.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?)", (i, f"s-{source}", role, content, stamp))
    db.commit()
    db.close()


def test_day_messages_are_marcos_own_across_sessions_and_skip_machine_text(tmp_path):
    day = datetime.date(2026, 11, 10)
    at = lambda h: datetime.datetime(2026, 11, 10, h, 5).timestamp()
    _db(tmp_path, [
        ("telegram", "user", "mergea la 1460", at(9)),
        ("desktop", "user", "<memory-context>x</memory-context>despliega a DES", at(10)),
        ("telegram", "assistant", "hecho", at(10)),
        ("cron", "user", "tarea de cron", at(10)),
        ("tool", "user", "## Goal review a PR", at(10)),
        ("telegram", "user", "[IMPORTANT: Background process done]", at(11)),
        ("telegram", "user", "[Cron delivery: Teams] resumen", at(11)),
        ("telegram", "user", "mensaje del turno actual", at(12)),
        ("telegram", "user", "mensaje de ayer", datetime.datetime(2026, 11, 9, 12, 0).timestamp()),
    ])
    text = reviewer.day_messages(str(tmp_path), day, "mensaje del turno actual")
    assert text.splitlines() == ["09:05 mergea la 1460", "10:05 despliega a DES"]


def test_day_messages_fail_open(tmp_path):
    assert reviewer.day_messages(str(tmp_path / "missing"), datetime.date(2026, 11, 10)) == ""


def test_with_earlier_orders_day_then_earlier_then_the_turn():
    text = reviewer.with_earlier("e", "t", "d")
    assert text.index("TODAY'S USER MESSAGES") < text.index("EARLIER TURNS") < text.index("THIS TURN") and text.endswith("t")
    assert reviewer.with_earlier("", "t", "") == "t"


def test_review_sends_the_day_as_background_but_only_the_turn_as_evidence(wired, monkeypatch):
    job, writes, sent = wired
    seen = []
    monkeypatch.setattr(reviewer, "day_messages", lambda home, now, current: "09:00 mergea la 1460")
    job.update(turn="[USER]\nsí, hazlo", earlier="", user_message="sí, hazlo")

    class Spy(FakeLlm):
        def complete_structured(self, instructions, input, json_schema, **kwargs):
            seen.append(input[0]["text"])
            return super().complete_structured(instructions, input, json_schema, **kwargs)
    llm = Spy([_change("add", "Martes 10", "Servicio: PR #1460 en DES", "✅", "Nada", "09:00 mergea la 1460")])
    assert reviewer.review(llm, job, now=datetime.date(2026, 11, 10)) == [] and not writes
    assert "09:00 mergea la 1460" in seen[0]


def _board(*rows):
    body = "".join(f"| {state} | {task} | {nxt} |\n" for state, task, nxt in rows)
    return policy.parse_note(NOTE.replace("| ⚪ |  |  |\n\n## 📅 Miércoles", body + "\n## 📅 Miércoles", 1))


@pytest.mark.parametrize("live,new", [
    ("Servicio: regenerar la guía social v1.0 (mic#1521)", "Servicio: regenerar la guía con los builders del repo (mic#1521)"),
    ("Servicio: regenerar la guía (mic#1521)", "Servicio: revisión de la guía (#1521)"),
    ("Servicio: alta en métricas de uso (TICKET-902)", "Servicio: respuesta de plataforma, TICKET-902"),
    ("Servicio: acelerar el CI (mic#1446, #1449 y spa#589 mergeadas)", "Servicio: despliegue de spa#589"),
])
def test_an_add_that_shares_a_reference_with_a_live_row_is_dropped(live, new):
    days = _board(("⏳", live, "Esperar"))
    add = _change("add", "Martes 10", new, "🔄", "Revisar", "PR #1460 mergeada")
    assert policy.gate([add], days, "Martes 10", CONTEXT) == []


@pytest.mark.parametrize("live,new", [
    ("Servicio: guía (mic#1521)", "Servicio: otra cosa (spa#1521)"),
    ("Servicio: guía (mic#1521)", "Servicio: otra cosa (mic#15210)"),
    ("Servicio: guía (mic#1521)", "Servicio: otra cosa (mic#152)"),
    ("Servicio: alta (TICKET-902)", "Servicio: otra cosa (TICKET-9020)"),
    ("Servicio: guía (mic#1521)", "Servicio: PR #1460 en DES"),
])
def test_references_that_differ_do_not_block_an_add(live, new):
    days = _board(("⏳", live, "Esperar"))
    add = _change("add", "Martes 10", new, "🔄", "Revisar", "PR #1460 mergeada")
    assert len(policy.gate([add], days, "Martes 10", CONTEXT)) == 1


def test_a_move_may_share_its_reference_with_the_row_it_replaces():
    days = policy.parse_note(NOTE.replace("Servicio: alta en Application Insights |", "Servicio: alta en Application Insights (TICKET-902) |", 1))
    task = "Servicio: alta en Application Insights (TICKET-902)"
    move = [_change("remove", "Lunes 9", task, "⏳", "x", "estamos dados de alta"),
            _change("add", "Martes 10", task, "⏳", "Confirmar acceso", "estamos dados de alta")]
    assert [c["op"] for c in policy.gate(move, days, "Martes 10", CONTEXT)] == ["remove", "add"]
