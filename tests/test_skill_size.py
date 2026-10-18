import sys

import pytest

guards = sys.modules["agent_guards"]
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


def pre(tool, **args):
    return guards._pre_tool_call(tool_name=tool, args=args, session_id="s1")


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
