import os
import subprocess
import sys
from pathlib import Path

import pytest

guards = sys.modules["shell_guards"]
size = guards.skill_size

SMALL = "---\nname: demo\ndescription: Use when testing.\n---\n" + "x" * 100


@pytest.fixture
def root(tmp_path, monkeypatch):
    skills = tmp_path / "skills"
    (skills / "cat" / "demo" / "references").mkdir(parents=True)
    (skills / "cat" / "demo" / "SKILL.md").write_text(SMALL)
    (skills / "cat" / "demo" / "references" / "topic.md").write_text("r" * 100)
    monkeypatch.setattr(size, "ROOTS", [skills])
    return skills


@pytest.fixture(autouse=True)
def _clear_caches():
    for cache in (getattr(size, "_PENDING", None), getattr(size, "_TREE", None),
                  getattr(size, "_FLAGGED", None)):
        if cache is not None:
            cache.clear()
    yield
    for cache in (getattr(size, "_PENDING", None), getattr(size, "_TREE", None),
                  getattr(size, "_FLAGGED", None)):
        if cache is not None:
            cache.clear()


def pre(tool, **args):
    return guards._pre_tool_call(tool_name=tool, args=args, session_id="s1")


def write(tool, result="ok", call_id="c1", **args):
    """The hook pair one writing tool call goes through: pre_tool_call, then the result transform.

    ``None`` is the unchanged result (every transform declined), which the host turns back
    into the original string; a returned string replaces it.
    """
    guards._pre_tool_call(tool_name=tool, args=args, session_id="s1", tool_call_id=call_id)
    return guards._transform_tool_result(tool_name=tool, args=args, result=result,
                                         session_id="s1", tool_call_id=call_id)


def flagged(out):
    return bool(out) and "over the limit for SKILL.md" in out and "references/<topic>.md" in out


def unchanged(out):
    return out is None


def out_has_char_count(out, count):
    return bool(out) and f"at {count:,} characters" in out


def blocked(result):
    return bool(result) and result.get("action") == "block" and result.get("message", "").startswith("agent-guards blocked")


def test_skill_manage_patch_over_limit_blocked(root):
    result = pre("skill_manage", action="patch", name="demo", old_string="x" * 10, new_string="y" * 7000)
    assert blocked(result) and "SKILL.md would be" in result["message"] and "references/<topic>.md" in result["message"]


def test_skill_manage_patch_within_limit_passes(root):
    assert pre("skill_manage", action="patch", name="demo", old_string="x" * 10, new_string="y" * 50) is None


def test_reference_limit_is_separate(root):
    ok = pre("skill_manage", action="write_file", name="demo", file_path="references/topic.md", file_content="r" * 11000)
    assert ok is None
    result = pre("skill_manage", action="write_file", name="demo", file_path="references/topic.md", file_content="r" * 12001)
    assert blocked(result) and "skill reference file" in result["message"]


def test_create_counts_new_skill_by_category(root):
    result = pre("skill_manage", action="create", name="fresh", category="cat", content="z" * 6001)
    assert blocked(result)
    assert pre("skill_manage", action="create", name="fresh", category="cat", content="z" * 6000) is None


def test_batch_operations_are_simulated_in_order(root):
    ops = [
        {"action": "patch", "name": "demo", "old_string": "x" * 10, "new_string": "y" * 4000},
        {"action": "patch", "name": "demo", "old_string": "x" * 10, "new_string": "y" * 4000},
    ]
    assert blocked(pre("skill_manage", operations=ops))
    assert pre("skill_manage", operations=ops[:1]) is None


def test_shrinking_an_oversized_file_is_allowed(root):
    skill = root / "cat" / "demo" / "SKILL.md"
    skill.write_text("x" * 20000)
    assert pre("skill_manage", action="patch", name="demo", old_string="x" * 5000, new_string="") is None
    assert pre("skill_manage", action="patch", name="demo", old_string="x" * 10, new_string="x" * 11) is not None


def test_replace_all_multiplies_growth(root):
    skill = root / "cat" / "demo" / "SKILL.md"
    skill.write_text("ab " * 1900)
    assert pre("skill_manage", action="patch", name="demo", old_string="ab", new_string="abc") is None
    assert blocked(pre("skill_manage", action="patch", name="demo", old_string="ab", new_string="abc", replace_all=True))


def test_file_tools_inside_skill_roots(root):
    skill = root / "cat" / "demo" / "SKILL.md"
    assert blocked(pre("write_file", path=str(skill), content="w" * 6001))
    assert blocked(pre("patch", path=str(skill), old_string="x" * 10, new_string="y" * 6000))
    assert pre("patch", path=str(skill), mode="patch", patch="whatever") is None
    assert pre("write_file", path=str(root / "cat" / "demo" / "scripts" / "big.py"), content="p" * 50000) is None


def test_files_outside_roots_and_archives_untouched(root, tmp_path):
    assert pre("write_file", path=str(tmp_path / "notes.md"), content="n" * 50000) is None
    archived = root / ".archive" / "old" / "SKILL.md"
    assert pre("write_file", path=str(archived), content="n" * 50000) is None


def test_symlinked_root_is_resolved(root, tmp_path, monkeypatch):
    link = tmp_path / "linked"
    link.symlink_to(root)
    monkeypatch.setattr(size, "ROOTS", [link])
    assert blocked(pre("write_file", path=str(root / "cat" / "demo" / "SKILL.md"), content="w" * 6001))


def test_other_tools_and_bad_args_pass(root):
    assert pre("read_file", path=str(root / "cat" / "demo" / "SKILL.md")) is None
    assert guards._pre_tool_call(tool_name="skill_manage", args="nope", session_id="s1") is None
    assert pre("skill_manage", action="delete", name="demo") is None


def test_path_through_symlink_is_resolved(root, tmp_path):
    link = tmp_path / "skill-link"
    link.symlink_to(root / "cat" / "demo")
    assert blocked(pre("write_file", path=str(link / "SKILL.md"), content="w" * 6001))


def test_unified_diff_patch_is_not_measured_even_when_oversized(root):
    skill = root / "cat" / "demo" / "SKILL.md"
    skill.write_text("x" * 20000)
    assert pre("patch", path=str(skill), mode="patch", patch="--- a\n+++ b\n") is None


def test_roots_follow_hermes_home(monkeypatch, tmp_path):
    import importlib

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    assert size._hermes_home() == tmp_path / "hh"


def test_terminal_append_past_limit_is_flagged(root):
    skill = root / "cat" / "demo" / "SKILL.md"
    guards._pre_tool_call(tool_name="terminal", args={"command": "cat >> x"}, session_id="s1", tool_call_id="c1")
    skill.write_text(skill.read_text() + "y" * 7000)
    out = guards._transform_tool_result(tool_name="terminal", args={"command": "cat >> x"}, result="ok",
                                        session_id="s1", tool_call_id="c1")
    assert flagged(out) and out.startswith("ok")


def test_execute_code_new_over_limit_reference_is_flagged(root):
    guards._pre_tool_call(tool_name="execute_code", args={"code": "open(...)"}, session_id="s1", tool_call_id="c2")
    (root / "cat" / "demo" / "references" / "fresh.md").write_text("r" * 12001)
    out = guards._transform_tool_result(tool_name="execute_code", args={"code": "open(...)"}, result="ok",
                                        session_id="s1", tool_call_id="c2")
    assert out and "over the limit for a skill reference file" in out


def test_shrinking_an_over_limit_file_is_not_flagged(root):
    skill = root / "cat" / "demo" / "SKILL.md"
    skill.write_text("x" * 9000)
    guards._pre_tool_call(tool_name="terminal", args={"command": "python shrink.py"}, session_id="s1",
                          tool_call_id="c1")
    skill.write_text("x" * 5000)
    assert guards._transform_tool_result(tool_name="terminal", args={"command": "python shrink.py"}, result="ok",
                                         session_id="s1", tool_call_id="c1") is None
    assert skill.read_text() == "x" * 5000


def test_write_under_the_limit_is_not_flagged(root):
    assert unchanged(write("terminal", command="cat >> x"))


def test_non_skill_markdown_is_not_flagged(root, tmp_path):
    notes = tmp_path / "notes.md"
    guards._pre_tool_call(tool_name="terminal", args={"command": "echo > notes.md"}, session_id="s1", tool_call_id="c3")
    notes.write_text("n" * 50000)
    assert guards._transform_tool_result(tool_name="terminal", args={"command": "echo > notes.md"}, result="ok",
                                         session_id="s1", tool_call_id="c3") is None


def test_internal_error_fails_open(root, monkeypatch):
    monkeypatch.setattr(size, "_snapshot", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert unchanged(write("terminal", command="cat >> x"))
    assert guards._pre_tool_call(tool_name="terminal", args={"command": "cat >> x"},
                                 session_id="s1", tool_call_id="c4") is None


def test_unified_diff_patch_is_measured_after_the_fact(root):
    skill = root / "cat" / "demo" / "SKILL.md"
    guards._pre_tool_call(tool_name="patch", args={"path": str(skill), "mode": "patch"}, session_id="s1",
                          tool_call_id="c5")
    skill.write_text("x" * 9000)
    out = guards._transform_tool_result(tool_name="patch", args={"path": str(skill), "mode": "patch"}, result="ok",
                                        session_id="s1", tool_call_id="c5")
    assert flagged(out)


def test_unrelated_tool_and_unpaired_transform_pass(root):
    assert unchanged(write("read_file", path=str(root / "cat" / "demo" / "SKILL.md")))
    assert guards._transform_tool_result(tool_name="terminal", args={}, result="ok", tool_call_id="never") is None


def test_non_string_result_untouched(root):
    (root / "cat" / "demo" / "SKILL.md").write_text("x" * 9000)
    guards._pre_tool_call(tool_name="terminal", args={"command": "cat >> x"}, session_id="s1", tool_call_id="c6")
    assert guards._transform_tool_result(tool_name="terminal", args={"command": "cat >> x"}, result={"ok": True},
                                         session_id="s1", tool_call_id="c6") is None


def test_untouched_over_limit_file_is_not_flagged(root):
    (root / "cat" / "demo" / "references" / "big.md").write_text("r" * 13000)
    assert unchanged(write("terminal", command="echo hi"))


def test_growth_that_stays_under_the_limit_is_not_flagged(root):
    skill = root / "cat" / "demo" / "SKILL.md"
    guards._pre_tool_call(tool_name="terminal", args={"command": "cat >> x"}, session_id="s1", tool_call_id="c7")
    skill.write_text(skill.read_text() + "y" * 200)
    assert guards._transform_tool_result(tool_name="terminal", args={"command": "cat >> x"}, result="ok",
                                         session_id="s1", tool_call_id="c7") is None


def test_repeated_transform_for_one_call_flags_once(root):
    skill = root / "cat" / "demo" / "SKILL.md"
    guards._pre_tool_call(tool_name="terminal", args={"command": "cat >> x"}, session_id="s1", tool_call_id="c8")
    skill.write_text("x" * 9000)
    args = {"command": "cat >> x"}
    first = guards._transform_tool_result(tool_name="terminal", args=args, result="ok", session_id="s1",
                                          tool_call_id="c8")
    second = guards._transform_tool_result(tool_name="terminal", args=args, result="ok", session_id="s1",
                                           tool_call_id="c8")
    assert flagged(first) and second is None


def test_multibyte_over_byte_limit_but_under_char_limit_is_not_flagged(root):
    skill = root / "cat" / "demo" / "SKILL.md"
    guards._pre_tool_call(tool_name="terminal", args={"command": "cat > x"}, session_id="s1", tool_call_id="c11")
    skill.write_text("\u00e9" * 5000)
    assert size.SKILL_LIMIT < len(skill.read_bytes()) and len(skill.read_text(encoding="utf-8")) < size.SKILL_LIMIT
    out = guards._transform_tool_result(tool_name="terminal", args={"command": "cat > x"}, result="ok",
                                        session_id="s1", tool_call_id="c11")
    assert out is None


def test_multibyte_file_over_char_and_byte_limit_is_flagged_with_char_count(root):
    skill = root / "cat" / "demo" / "SKILL.md"
    guards._pre_tool_call(tool_name="terminal", args={"command": "cat > x"}, session_id="s1", tool_call_id="c12")
    skill.write_text("\u00e9" * 7000)
    assert out_has_char_count(guards._transform_tool_result(
        tool_name="terminal", args={"command": "cat > x"}, result="ok", session_id="s1", tool_call_id="c12"), 7000)


def test_untouched_multibyte_over_limit_file_is_not_flagged(root):
    (root / "cat" / "demo" / "references" / "big.md").write_text("\u00e9" * 13000)
    assert unchanged(write("terminal", command="echo hi"))


def test_post_hoc_and_pre_call_agree_on_units_for_a_multibyte_file(root):
    skill = root / "cat" / "demo" / "SKILL.md"
    body = "\u00e9" * 5985
    skill.write_text(body)
    assert len(body) == 5985 and len(body.encode("utf-8")) > size.SKILL_LIMIT
    assert pre("write_file", path=str(skill), content=body) is None
    guards._pre_tool_call(tool_name="terminal", args={"command": "true"}, session_id="s1", tool_call_id="u1")
    skill.write_text(body + "x")
    snapshot = size._snapshot()
    assert snapshot[str(skill)][1] == len(body + "x") == 5986
    assert unchanged(guards._transform_tool_result(tool_name="terminal", args={"command": "true"}, result="ok",
                                                   session_id="s1", tool_call_id="u1"))


def test_every_file_left_over_its_limit_is_flagged(root):
    skill = root / "cat" / "demo" / "SKILL.md"
    reference = root / "cat" / "demo" / "references" / "fresh.md"
    guards._pre_tool_call(tool_name="terminal", args={"command": "python gen.py"}, session_id="s1",
                          tool_call_id="c13")
    skill.write_text("x" * 9000)
    reference.write_text("r" * 13000)
    out = guards._transform_tool_result(tool_name="terminal", args={"command": "python gen.py"}, result="ok",
                                        session_id="s1", tool_call_id="c13")
    assert out.startswith("ok")
    assert "SKILL.md" in out and "fresh.md" in out
    assert out.count("agent-guards: terminal left") == 2


def test_directory_cache_does_not_hide_a_new_file(root):
    guards._pre_tool_call(tool_name="terminal", args={"command": "true"}, session_id="s1", tool_call_id="c9")
    guards._transform_tool_result(tool_name="terminal", args={"command": "true"}, result="ok", session_id="s1",
                                  tool_call_id="c9")
    guards._pre_tool_call(tool_name="terminal", args={"command": "cat > x"}, session_id="s1", tool_call_id="c10")
    (root / "cat" / "demo" / "references" / "added.md").write_text("r" * 12001)
    out = guards._transform_tool_result(tool_name="terminal", args={"command": "cat > x"}, result="ok",
                                        session_id="s1", tool_call_id="c10")
    assert out and "added.md" in out


def test_symlinked_directory_out_of_the_roots_is_not_snapshotted(root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escaped.md").write_text("x" * 50000)
    (root / "cat" / "demo" / "escape").symlink_to(outside, target_is_directory=True)
    snapshot = size._snapshot()
    assert all(Path(os.path.realpath(path)).is_relative_to(root) for path in snapshot)
    assert unchanged(write("terminal", command="cat > escaped.md"))


def test_pending_snapshot_is_bounded(root):
    for index in range(size._MAX_PENDING + 3):
        guards._pre_tool_call(tool_name="terminal", args={"command": "true"}, session_id="s1",
                              tool_call_id=f"c{index}")
    assert len(size._PENDING) == size._MAX_PENDING


def _grow(root, *, path=None, body="y" * 7000):
    """A watched terminal call that leaves one skill file oversized."""
    skill = path or (root / "cat" / "demo" / "SKILL.md")
    guards._pre_tool_call(tool_name="terminal", args={"command": "cat >> x"}, session_id="s1",
                          tool_call_id="m1")
    skill.write_text(skill.read_text() + body)
    return guards._transform_tool_result(tool_name="terminal", args={"command": "cat >> x"},
                                         result="ok", session_id="s1", tool_call_id="m1")


def test_message_leads_with_the_required_action_in_one_imperative_sentence(root):
    out = _grow(root)
    lead = out[len("ok"):].strip().split(". ")[0]
    assert lead.startswith("Shrink "), lead
    assert "IMMEDIATELY, in this turn" in lead
    assert "MANDATORY" in lead and "not a suggestion" in lead
    assert "It is over the limit NOW" not in lead, f"the lead must be ONE sentence: {lead!r}"


def test_message_says_over_the_limit_now_and_cause_does_not_matter(root):
    out = _grow(root)
    assert "It is over the limit NOW" in out
    assert "regardless of what caused the growth" in out
    assert "who caused it does not matter" in out


def test_message_says_shrinking_is_always_allowed(root):
    assert "Shrinking an over-limit file is always allowed" in _grow(root)


def test_message_forbids_postponing_or_asking_permission(root):
    assert "do not postpone it or ask for permission" in _grow(root)


def test_message_forbids_the_unrelated_claim(root):
    out = _grow(root)
    assert "Do not reply that the file is unrelated to your task" in out


def test_message_keeps_the_tree_split_instruction(root):
    out = _grow(root)
    assert "Distribute the content as a tree instead" in out
    assert "references/<topic>.md" in out and "Move text verbatim" in out


def test_message_states_the_limit_and_the_char_count(root):
    out = _grow(root)
    assert "over the limit for SKILL.md" in out
    assert "at 7,150 characters" in out


def _verify(**kwargs):
    """The host's ``pre_verify`` entry point for this plugin."""
    kwargs.setdefault("session_id", "s1")
    return guards._pre_verify(**kwargs)


def test_pre_verify_continues_while_the_flagged_file_is_over_the_limit(root):
    _grow(root)
    directive = _verify()
    assert directive and directive["action"] == "continue"
    assert "SKILL.md" in directive["message"]
    assert "still over its size limit" in directive["message"]


def test_pre_verify_clears_silently_once_the_file_is_fixed(root):
    _grow(root)
    assert _verify() is not None
    (root / "cat" / "demo" / "SKILL.md").write_text(SMALL)
    assert _verify() is None
    assert "s1" not in size._FLAGGED


def test_pre_verify_stops_after_the_cap(root):
    _grow(root)
    for _ in range(size._MAX_CONTINUATIONS):
        assert _verify() is not None
    assert _verify() is None, "the continuation must be capped per file per session"


def test_pre_verify_never_fires_for_a_file_that_was_never_flagged(root):
    (root / "cat" / "demo" / "SKILL.md").write_text("x" * 9000)
    assert _verify() is None
    assert _verify(session_id="another") is None


def test_pre_verify_is_scoped_to_the_session_that_saw_the_flag(root):
    _grow(root)
    assert _verify(session_id="other") is None
    assert _verify(session_id="s1") is not None


def test_pre_verify_fails_open_on_internal_errors(root, monkeypatch):
    _grow(root)
    monkeypatch.setattr(size, "_over_limit", lambda _p: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _verify() is None


def test_the_concurrent_writer_case_is_enforced_and_cleared(root):
    """Card step 1: a second writer grows the file during an unrelated call.

    The flag is produced (false attribution, unchanged) — but now the turn cannot end
    while the file is still over its limit, and the flag clears once it is fixed.
    """
    guards._pre_tool_call(tool_name="terminal", args={"command": "echo hi"}, session_id="s1",
                          tool_call_id="w1")
    skill = root / "cat" / "demo" / "SKILL.md"
    subprocess.run([sys.executable, "-c", f"open({str(skill)!r}, 'a').write('y' * 7000)"], check=True)
    out = guards._transform_tool_result(tool_name="terminal", args={"command": "echo hi"}, result="hi",
                                        session_id="s1", tool_call_id="w1")
    assert out and "over the limit" in out
    assert _verify() is not None, "the flag must now be enforced, not merely reported"
    skill.write_text(SMALL)
    assert _verify() is None


def test_flagged_file_removed_from_disk_clears_the_flag(root):
    _grow(root)
    (root / "cat" / "demo" / "SKILL.md").unlink()
    assert _verify() is None


def test_flagging_is_bounded_per_session(root):
    for index in range(size._MAX_FLAGGED_SESSIONS + 2):
        size._remember_flag(f"s{index}", "/x/SKILL.md")
    assert len(size._FLAGGED) == size._MAX_FLAGGED_SESSIONS
