"""unused_code_gate: the diff a card asks review for cannot add unused private code.

The repo rule is 'unused code is not allowed, even if it does nothing'. The real
incident: a worker added ``_PYTH_DELETE``/``_PYTH_ABS_DELETE`` constants, review
approved the PR, and only afterwards the lead made the worker remove them. This
gate catches names the diff ADDS that nothing references, at request_review/complete
time instead.

These tests drive the gate over throwaway git repos; the real repo is never touched.
"""

import os
import subprocess
import sys

import pytest

guards = sys.modules["kanban_guards"]
gate = guards.unused_code_gate


@pytest.fixture(autouse=True)
def _gate_active(monkeypatch):
    """The gate only runs for a worker on a card, in a configured-worker setup."""
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_probe")
    yield


def _git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)


def _repo(tmp_path, files, remote=True):
    """A git repo with one commit; ``files`` maps relative paths to content."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    if remote:
        bare = tmp_path / "origin.git"
        _git(tmp_path, "init", "-q", "--bare", str(bare))
        _git(repo, "remote", "add", "origin", str(bare))
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "push", "-q", "origin", "main") if remote else None
    return repo


def _commit(repo, files):
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")


def _modify(repo, files):
    """Uncommitted changes on top of the committed state."""
    for name, content in files.items():
        path = repo / name
        if content is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)


def _call(monkeypatch, repo, tool="kanban_request_review", branch="main"):
    monkeypatch.setattr(gate, "_workspace_path", lambda: str(repo))
    return gate.check(tool, {})


def test_registers_new_private_constant_blocked(tmp_path, monkeypatch):
    """The real incident: unreferenced module-level _CONSTANTS added by the diff."""
    repo = _repo(tmp_path, {"pkg/__init__.py": "", "pkg/api.py": "def call():\n    return 1\n"})
    _commit(repo, {"pkg/api.py": "def call():\n    return 1\n\n_PYTH_DELETE = None\n_PYTH_ABS_DELETE = None\n"})
    blocked = _call(monkeypatch, repo)
    assert blocked and "pkg/api.py" in blocked
    for name in ("_PYTH_DELETE", "_PYTH_ABS_DELETE"):
        assert name in blocked and "3" in blocked or name in blocked


def test_block_message_names_file_line_and_exact_fix(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"pkg/api.py": "x = 1\n"})
    _commit(repo, {"pkg/api.py": "x = 1\n\n_UNUSED = 2\n"})
    blocked = _call(monkeypatch, repo)
    assert blocked and "pkg/api.py:3:_UNUSED" in blocked
    assert "delete" in blocked.lower() and "test" in blocked.lower()


def test_private_name_referenced_in_same_module_passes(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"pkg/api.py": "def use():\n    return _helper()\n"})
    _commit(repo, {"pkg/api.py": "def use():\n    return _helper()\n\n\ndef _helper():\n    return 1\n"})
    assert _call(monkeypatch, repo) is None


def test_private_name_referenced_only_by_test_file_passes(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"pkg/mod.py": ""})
    _commit(repo, {"pkg/mod.py": "_SECRET = 1\n", "tests/test_mod.py": "from pkg.mod import _SECRET\n\n\ndef test_secret():\n    assert _SECRET == 1\n"})
    assert _call(monkeypatch, repo) is None


def test_private_name_listed_in_all_passes(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"pkg/mod.py": ""})
    _commit(repo, {"pkg/mod.py": "_HIDDEN = 1\n\n__all__ = ['_HIDDEN']\n"})
    assert _call(monkeypatch, repo) is None


def test_dunder_names_pass(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"pkg/mod.py": ""})
    _commit(repo, {"pkg/mod.py": "_VERSION = '1'\n\n__version__ = _VERSION\n"})
    assert _call(monkeypatch, repo) is None


def test_name_that_already_existed_before_the_diff_passes(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"pkg/mod.py": "_OLD = 1\n"})
    _commit(repo, {"pkg/mod.py": "_OLD = 1\n\ndef _new_unused():\n    return 2\n"})
    blocked = _call(monkeypatch, repo)
    assert blocked and "_OLD" not in blocked and "_new_unused" in blocked


def test_renamed_too_uses_git_grep_word_match(tmp_path, monkeypatch):
    """git grep -w keeps a word-boundary reference from hiding the unused name."""
    repo = _repo(tmp_path, {"pkg/mod.py": ""})
    _commit(repo, {"pkg/mod.py": "_RATE = 1\n", "pkg/other.py": "limit = _RATE_LIMIT\n"})
    blocked = _call(monkeypatch, repo)
    assert blocked and "_RATE" in blocked


def test_diff_with_no_python_files_passes(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"README.md": "a\n"})
    _commit(repo, {"README.md": "b\n", "app.js": "let _unused = 1\n"})
    assert _call(monkeypatch, repo) is None


def test_git_unavailable_fails_open(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"pkg/mod.py": "x = 1\n"})
    _commit(repo, {"pkg/mod.py": "x = 1\n\n_UNUSED = 2\n"})
    monkeypatch.setattr(gate, "_which_git", lambda: None)
    assert _call(monkeypatch, repo) is None


def test_git_timeout_fails_open(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"pkg/mod.py": "x = 1\n"})
    _commit(repo, {"pkg/mod.py": "x = 1\n\n_UNUSED = 2\n"})

    def slow(cmd, cwd):
        import subprocess as sp

        raise sp.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(gate, "_run", slow)
    assert _call(monkeypatch, repo) is None


def test_internal_exception_fails_open(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"pkg/mod.py": "x = 1\n"})
    _commit(repo, {"pkg/mod.py": "x = 1\n\n_UNUSED = 2\n"})
    monkeypatch.setattr(gate, "_diff_added_names", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _call(monkeypatch, repo) is None


def test_syntax_error_in_changed_file_fails_open(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"pkg/mod.py": ""})
    _commit(repo, {"pkg/mod.py": "_UNUSED = 1\ndef broken(:\n"})
    assert _call(monkeypatch, repo) is None


def test_non_worker_or_non_card_process_passes(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"pkg/mod.py": "x = 1\n"})
    _commit(repo, {"pkg/mod.py": "x = 1\n\n_UNUSED = 2\n"})
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: None)
    assert _call(monkeypatch, repo) is None


def test_other_tool_names_pass(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"pkg/mod.py": "x = 1\n"})
    _commit(repo, {"pkg/mod.py": "x = 1\n\n_UNUSED = 2\n"})
    assert _call(monkeypatch, repo, tool="kanban_comment") is None


def test_uncommitted_changes_count(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"pkg/mod.py": "x = 1\n"})
    _modify(repo, {"pkg/mod.py": "x = 1\n\n_UNUSED_NOW = 2\n"})
    blocked = _call(monkeypatch, repo)
    assert blocked and "_UNUSED_NOW" in blocked


def test_uncommitted_deletion_of_reference_passes(tmp_path, monkeypatch):
    """A deletion adds no name, so removing the last caller of an existing private
    function is pre-existing-code hygiene, not a diff-added unused name."""
    repo = _repo(tmp_path, {"pkg/mod.py": "def _helper():\n    return 1\n\n\ndef call():\n    return _helper()\n"})
    _modify(repo, {"pkg/mod.py": "def _helper():\n    return 1\n"})
    blocked = _call(monkeypatch, repo)
    assert blocked is None


def test_complete_tool_is_gated_too(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"pkg/mod.py": "x = 1\n"})
    _commit(repo, {"pkg/mod.py": "x = 1\n\n_UNUSED = 2\n"})
    assert _call(monkeypatch, repo, tool="kanban_complete")


def test_imports_added_but_unused_blocked(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"pkg/mod.py": "x = 1\n"})
    _commit(repo, {"pkg/mod.py": "import os\nimport sys\n\nx = os.getcwd()\n"})
    blocked = _call(monkeypatch, repo)
    assert blocked and "sys" in blocked and "pkg/mod.py" in blocked


def test_import_reexported_in_package_init_passes(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"pkg/__init__.py": "", "pkg/thing.py": "x = 1\n"})
    _commit(repo, {"pkg/thing.py": "from os import getcwd\n", "pkg/__init__.py": "from pkg.thing import getcwd\n"})
    assert _call(monkeypatch, repo) is None


def test_import_listed_in_all_passes(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"pkg/mod.py": ""})
    _commit(repo, {"pkg/mod.py": "from os import getcwd\n\n__all__ = ['getcwd']\n"})
    assert _call(monkeypatch, repo) is None


def test_registered_on_request_review_and_complete_under_canonical_name():
    names = [name for name, _ in guards._PRE_TOOL_CALL_CHECKS]
    assert "unused_code_gate" in names
    import inspect

    source = inspect.getsource(gate.check)
    for tool in ("kanban_request_review", "kanban_complete"):
        assert tool in source
