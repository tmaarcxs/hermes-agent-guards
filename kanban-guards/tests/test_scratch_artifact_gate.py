import os
import subprocess
import sys

import pytest

guards = sys.modules["kanban_guards"]
gate = guards.scratch_artifact_gate


@pytest.fixture(autouse=True)
def worker_process(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_scratch1")


def _register_scratch(task_id, root):
    """Point the gate's task lookup at a scratch card with workspace root."""
    gate._task = lambda _task_id: {"workspace_kind": "scratch", "workspace_path": str(root)}
    return root


def _register_other(task_id, kind, root):
    gate._task = lambda _task_id: {"workspace_kind": kind, "workspace_path": str(root)}


@pytest.fixture(autouse=True)
def restore_lookup():
    original = gate._task
    yield
    gate._task = original


def _check(args, tool="kanban_complete", task_id="t_scratch1"):
    return gate.check(tool, {"task_id": task_id, **args})


def test_overview(tmp_path):
    assert gate.__doc__


def test_blocked_summary_cites_scratch_path(tmp_path):
    root = _register_scratch("t_scratch1", tmp_path)
    (root / "report.md").write_text("x")
    message = _check({"summary": f"See {root}/report.md for the full analysis.", "metadata": {}})
    assert message is not None
    assert "agent-guards blocked" in message
    assert str(root / "report.md") in message
    assert "metadata" in message and "artifacts" in message
    assert "deleted" in message


def test_passes_when_path_covered_in_artifacts(tmp_path):
    root = _register_scratch("t_scratch1", tmp_path)
    (root / "report.md").write_text("x")
    message = _check({
        "summary": f"See {root}/report.md.",
        "metadata": {"artifacts": [f"{root}/report.md"]},
    })
    assert message is None


def test_passes_when_covered_by_containing_directory(tmp_path):
    root = _register_scratch("t_scratch1", tmp_path)
    (root / "out").mkdir()
    (root / "out" / "report.md").write_text("x")
    message = _check({
        "summary": f"See {root}/out/report.md.",
        "metadata": {"artifacts": [f"{root}/out"]},
    })
    assert message is None


def test_passes_when_covered_by_archive(tmp_path):
    root = _register_scratch("t_scratch1", tmp_path)
    (root / "report.md").write_text("x")
    (root / "out.tgz").write_text("x")
    message = _check({
        "summary": f"See {root}/out.tgz for the full report set.",
        "metadata": {"artifacts": [f"{root}/out.tgz"]},
    })
    assert message is None
    message = _check({
        "summary": f"See {root}/report.md; also shipped {root}/out.tgz.",
        "metadata": {"artifacts": [f"{root}/out.tgz"]},
    })
    assert message is not None


def test_uncovered_paths_only(tmp_path):
    root = _register_scratch("t_scratch1", tmp_path)
    (root / "report.md").write_text("x")
    (root / "covered.md").write_text("x")
    message = _check({
        "summary": f"See {root}/report.md and {root}/covered.md.",
        "metadata": {"artifacts": [f"{root}/covered.md"]},
    })
    assert message is not None
    assert str(root / "report.md") in message
    assert str(root / "covered.md") not in message.split("git bundle")[0]


def test_coverage_is_path_prefix_not_string_prefix(tmp_path):
    root = _register_scratch("t_scratch1", tmp_path)
    (root / "report.md").write_text("x")
    (root / "report.md.evil").write_text("x")
    message = _check({
        "summary": f"See {root}/report.md.evil and {root}/report.md.",
        "metadata": {"artifacts": [f"{root}/report.md"]},
    })
    assert message is not None
    assert str(root / "report.md.evil") in message


def test_metadata_values_count_as_handoff_text(tmp_path):
    root = _register_scratch("t_scratch1", tmp_path)
    (root / "d.png").write_text("x")
    message = _check({
        "summary": "Done.",
        "metadata": {"notes": f"diagram at {root}/d.png"},
    })
    assert message is not None
    assert str(root / "d.png") in message


def test_nested_metadata_values_count_as_handoff_text(tmp_path):
    root = _register_scratch("t_scratch1", tmp_path)
    (root / "d.png").write_text("x")
    message = _check({
        "summary": "Done.",
        "metadata": {"acceptance": {"1": f"evidence at {root}/d.png"}},
    })
    assert message is not None


def test_metadata_json_string_accepted(tmp_path):
    import json
    root = _register_scratch("t_scratch1", tmp_path)
    (root / "d.png").write_text("x")
    message = _check({
        "summary": "Done.",
        "metadata": json.dumps({"notes": f"diagram at {root}/d.png"}),
    })
    assert message is not None


def test_request_review_gated_too(tmp_path):
    root = _register_scratch("t_scratch1", tmp_path)
    (root / "report.md").write_text("x")
    (root / "report.md").write_text("x")
    message = _check({"summary": f"See {root}/report.md."}, tool="kanban_request_review")
    assert message is not None


def test_other_tools_pass_through(tmp_path):
    _register_scratch("t_scratch1", tmp_path)
    assert gate.check("kanban_block", {"reason": f"file at {tmp_path}/x.md"}) is None


def test_unpushed_head_git_repo_blocked(tmp_path):
    root = _register_scratch("t_scratch1", tmp_path)
    repo = root / "repo"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, env=env)
    run("init", "-q")
    (repo / "f.txt").write_text("a")
    run("add", "-A")
    run("commit", "-qm", "work")
    message = _check({"summary": "Done: work in repo.", "metadata": {}})
    assert message is not None
    assert "git bundle create" in message
    assert "deleted" in message


def test_unpushed_head_message_names_branch_and_sha(tmp_path):
    root = _register_scratch("t_scratch1", tmp_path)
    repo = root / "repo"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, env=env)
    run("init", "-q", "-b", "main")
    run("checkout", "-q", "-b", "work")
    (repo / "f.txt").write_text("a")
    run("add", "-A")
    run("commit", "-qm", "work")
    head = run("rev-parse", "HEAD").stdout.strip()
    message = _check({"summary": "Done.", "metadata": {}})
    assert message is not None
    assert head in message
    assert "work" in message
    assert "git bundle create" in message


def test_bundle_artifact_passes(tmp_path):
    root = _register_scratch("t_scratch1", tmp_path)
    repo = root / "repo"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, env=env)
    run("init", "-q")
    run("checkout", "-q", "-b", "work")
    (repo / "f.txt").write_text("a")
    run("add", "-A")
    run("commit", "-qm", "work")
    run("bundle", "create", str(root / "work.bundle"), "work")
    message = _check({"summary": "Done.", "metadata": {"artifacts": [str(root / "work.bundle")]}})
    assert message is None


def test_patch_artifacts_pass(tmp_path):
    root = _register_scratch("t_scratch1", tmp_path)
    repo = root / "repo"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    run = lambda *a: subprocess.run(["git", "init", "-q"] if a[0] == "init" else ["git", "-C", str(repo), *a], capture_output=True, text=True, env=env)
    run("init")
    run("checkout", "-b", "work")
    (repo / "f.txt").write_text("a")
    run("add", "-A")
    run("commit", "-qm", "work")
    (repo / "g.txt").write_text("b")
    run("add", "-A")
    run("commit", "-qm", "more")
    run("format-patch", "origin/master" if False else "--root", "-o", str(root))
    patches = sorted(p.name for p in root.glob("*.patch"))
    message = _check({"summary": "Done.", "metadata": {"artifacts": [str(root / p) for p in patches]}})
    assert message is None


def test_commits_on_remote_tracking_ref_pass(tmp_path):
    root = _register_scratch("t_scratch1", tmp_path)
    remote = tmp_path.parent / (tmp_path.name + "-remote")
    remote.mkdir()
    renv = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    bare = subprocess.run(["git", "init", "-q", "--bare", str(remote)], capture_output=True, text=True, env=renv)
    assert bare.returncode == 0
    repo = root / "repo"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, env=env)
    run("init", "-q")
    (repo / "f.txt").write_text("a")
    run("add", "-A")
    run("commit", "-qm", "work")
    run("remote", "add", "origin", str(remote))
    run("push", "-q", "origin", "HEAD:refs/heads/main")
    message = _check({"summary": "Done.", "metadata": {}})
    assert message is None


def test_non_scratch_workspaces_pass(tmp_path):
    for kind in ("worktree", "dir"):
        _register_other("t_scratch1", kind, tmp_path / kind)
        message = _check({"summary": f"See {tmp_path}/report.md.", "metadata": {}})
        assert message is None


def test_non_worker_setup_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")
    _register_scratch("t_scratch1", tmp_path)
    (tmp_path / "report.md").write_text("x")
    assert _check({"summary": f"See {tmp_path}/report.md.", "metadata": {}}) is None


def test_fail_open_on_internal_error(tmp_path, monkeypatch):
    _register_scratch("t_scratch1", tmp_path)
    monkeypatch.setattr(gate, "_scan_scratch_repos", lambda root: (_ for _ in ()).throw(RuntimeError("boom")))
    assert gate.check("kanban_complete", {"task_id": "t_scratch1", "summary": "x", "metadata": {}}) is None


def test_fail_open_when_task_lookup_raises(tmp_path, monkeypatch):
    def boom(_task_id):
        raise RuntimeError("db gone")
    gate._task = boom
    assert gate.check("kanban_complete", {"task_id": "t_scratch1", "summary": "x", "metadata": {}}) is None


def test_fail_open_when_task_missing(tmp_path):
    gate._task = lambda _task_id: None
    assert gate.check("kanban_complete", {"task_id": "t_scratch1", "summary": "x", "metadata": {}}) is None


def test_registered_on_pre_tool_call_for_both_tools():
    """The registered name is asserted in test_register_contract.py; here the gate
    proves its tools under the hook's own dispatch."""
    from kanban_guards import _PRE_TOOL_CALL_CHECKS
    names = [name for name, _ in _PRE_TOOL_CALL_CHECKS]
    assert "scratch_artifact_gate" in names


@pytest.mark.parametrize("tool", ["kanban_complete", "kanban_request_review"])
def test_registered_hook_gates_both_tools(tmp_path, tool):
    root = _register_scratch("t_scratch1", tmp_path)
    (root / "report.md").write_text("x")
    args = {"summary": f"See {root}/report.md.", "metadata": {}}
    from kanban_guards import _pre_tool_call
    blocked = _pre_tool_call(tool, args)
    assert blocked and blocked.get("action") == "block"
