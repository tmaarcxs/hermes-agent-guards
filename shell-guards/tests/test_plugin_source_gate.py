"""The plugin-source gate: a plugin is activated only from a pushed revision.

Every test builds a throwaway git repository under a temporary HERMES_HOME, so
the real plugins are never touched.
"""

import importlib.util
import subprocess
import sys

import pytest

from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from shell_guards import plugin_source_gate


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "plugins").mkdir()
    yield


def make_plugin(tmp_path: Path, name: str = "demo-plugin") -> Path:
    """A plugin dir that is a git checkout with an upstream and a clean tree."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    plugin = tmp_path / "plugins" / name
    plugin.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(plugin)], check=True)
    subprocess.run(["git", "-C", str(plugin), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(plugin), "config", "user.name", "t"], check=True)
    (plugin / "plugin.yaml").write_text("name: demo\n")
    subprocess.run(["git", "-C", str(plugin), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(plugin), "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", str(plugin), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(plugin), "push", "-q", "-u", "origin", "HEAD"], check=True)
    return plugin


# --- the activation gate ----------------------------------------------------------------------

def test_clean_and_pushed_plugin_activates(tmp_path):
    make_plugin(tmp_path)
    assert plugin_source_gate.activation_block("hermes plugins update demo-plugin") is None
    assert plugin_source_gate.activation_block("hermes plugins enable demo-plugin") is None


def test_uncommitted_change_blocks_activation(tmp_path):
    plugin = make_plugin(tmp_path)
    (plugin / "plugin.yaml").write_text("name: demo\n# edited\n")
    message = plugin_source_gate.activation_block("hermes plugins update demo-plugin")
    assert message and "UNCOMMITTED" in message
    assert "PUSH" in message


def test_untracked_file_blocks_activation(tmp_path):
    plugin = make_plugin(tmp_path)
    (plugin / "new.py").write_text("x = 1\n")
    assert plugin_source_gate.activation_block("hermes plugins enable demo-plugin") is not None


def test_committed_but_unpushed_blocks_activation(tmp_path):
    plugin = make_plugin(tmp_path)
    (plugin / "plugin.yaml").write_text("name: demo\n# v2\n")
    subprocess.run(["git", "-C", str(plugin), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(plugin), "commit", "-qm", "v2"], check=True)
    message = plugin_source_gate.activation_block("hermes plugins update demo-plugin")
    assert message and "not pushed" in message


def test_commit_then_push_allows_activation(tmp_path):
    plugin = make_plugin(tmp_path)
    (plugin / "plugin.yaml").write_text("name: demo\n# v2\n")
    subprocess.run(["git", "-C", str(plugin), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(plugin), "commit", "-qm", "v2"], check=True)
    assert plugin_source_gate.activation_block("hermes plugins update demo-plugin") is not None
    subprocess.run(["git", "-C", str(plugin), "push", "-q"], check=True)
    assert plugin_source_gate.activation_block("hermes plugins update demo-plugin") is None


def test_other_commands_are_untouched(tmp_path):
    plugin = make_plugin(tmp_path)
    (plugin / "plugin.yaml").write_text("name: demo\n# edited\n")
    assert plugin_source_gate.activation_block("hermes plugins list") is None
    assert plugin_source_gate.activation_block("git -C %s status" % plugin) is None
    assert plugin_source_gate.activation_block("") is None


def test_a_non_plugin_command_is_untouched(tmp_path):
    make_plugin(tmp_path)
    assert plugin_source_gate.activation_block("hermes plugins update some-other") is None


def test_check_only_gates_terminal(tmp_path):
    make_plugin(tmp_path)
    (tmp_path / "plugins" / "demo-plugin" / "plugin.yaml").write_text("name: demo\n# edited\n")
    assert plugin_source_gate.check("write_file", {"path": "x"}) is None
    assert plugin_source_gate.check(
        "terminal", {"command": "hermes plugins update demo-plugin"}
    ) is not None


def test_a_non_git_plugin_dir_is_untouched(tmp_path):
    plain = tmp_path / "plugins" / "plain"
    plain.mkdir()
    assert plugin_source_gate.is_git_checkout(plain) is False
    assert plugin_source_gate.activation_block("hermes plugins update plain") is None


def test_uncommitted_paths_reports_the_files(tmp_path):
    plugin = make_plugin(tmp_path)
    (plugin / "a.py").write_text("x = 1\n")
    paths = plugin_source_gate.uncommitted_paths(plugin)
    assert "a.py" in paths


def test_head_is_pushed_truth_table(tmp_path):
    plugin = make_plugin(tmp_path)
    assert plugin_source_gate.head_is_pushed(plugin) is True
    (plugin / "plugin.yaml").write_text("name: demo\n# v2\n")
    subprocess.run(["git", "-C", str(plugin), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(plugin), "commit", "-qm", "v2"], check=True)
    assert plugin_source_gate.head_is_pushed(plugin) is False


def test_a_repo_without_upstream_is_unknown(tmp_path):
    plugin = tmp_path / "plugins" / "no-upstream"
    plugin.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(plugin)], check=True)
    assert plugin_source_gate.head_is_pushed(plugin) is None
