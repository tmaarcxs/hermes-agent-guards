"""E2E: real plugin discovery, terminal execution, RTK pipe and private recovery."""

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import venv

import pytest

CORE = Path(os.environ.get("HERMES_RTK_E2E_CORE", Path.home() / ".hermes/hermes-agent"))
PLUGIN = Path(__file__).resolve().parents[1]
CONSUMER = Path(__file__).with_name("terminal_consumer.py")
SECRET = "sk-proj-" + "syntheticFixtureOnly" * 3


def run_scenario(root, mode, *, enabled=True):
    root.mkdir()
    home = root / "home"
    home.mkdir()
    installed = home / "plugins/rtk-output"
    installed.parent.mkdir()
    installed.symlink_to(PLUGIN, target_is_directory=True)
    enabled_plugins = ["rtk-output"] if enabled else []
    if mode in ("guard-growth", "guard-normal"):
        (home / "plugins/shell-guards").symlink_to(PLUGIN.parent / "shell-guards", target_is_directory=True)
        enabled_plugins.append("shell-guards")
        skill = home / "skills/demo/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: demo\ndescription: Use when testing.\n---\n")
    (home / "config.yaml").write_text(json.dumps({"plugins": {"enabled": enabled_plugins}}))
    testdir = root / "suite"
    testdir.mkdir()
    (testdir / "test_example.py").write_text(
        'import os, sys\nfrom pathlib import Path\nimport pytest\n'
        '@pytest.mark.parametrize("n", range(80))\ndef test_native(n):\n'
        '    assert "RTK_TEST_UNSET" not in os.environ\n'
        '    assert sys.prefix == os.environ["EXPECTED_PREFIX"]\n'
        '    if n == 0 and os.environ["SCENARIO"] == "guard-growth": Path("home/skills/demo/SKILL.md").write_text("x" * 7001)\n'
        '    with Path("executions").open("a") as f: f.write("once\\n")\n'
        '    assert not (os.environ["SCENARIO"] == "failure" and n == 79), "DIAGNOSTIC_SENTINEL"\n'
        '    print("fixture log " * 8)\n'
        f'    print({SECRET!r})\n'
    )
    virtual = root / ".venv"
    venv.EnvBuilder(system_site_packages=True).create(virtual)
    python = virtual / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    site_dir = Path(subprocess.check_output([str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip())
    (site_dir / "fixture-runtime.pth").write_text(str(Path(pytest.__file__).resolve().parents[1]) + "\n")
    command = f"env -u PYTHONPATH -u RTK_TEST_UNSET {shlex.quote(str(python))} -m pytest -v -s --color=no --tb=long suite"
    if mode == "quiet":
        command = command.replace("-v -s", "-q -s")
    if mode == "script":
        script = root / "runner.sh"
        script.write_text(command + "\n")
        command = f"bash {shlex.quote(str(script))}"
    if mode == "collection":
        (testdir / "test_broken.py").write_text("def invalid(:\n")
    if mode == "empty":
        (testdir / "test_example.py").unlink()
    if mode == "masked":
        (testdir / "test_broken.py").write_text("def invalid(:\n")
        command += "; true"
    if mode in ("unknown", "small"):
        script = root / "plain.py"
        script.write_text('print("OPAQUE_SENTINEL " * 500)' if mode == "unknown" else 'print("SHORT_SENTINEL")')
        command = f"{shlex.quote(str(python))} {shlex.quote(str(script))}"
    if mode == "approval":
        command = f"{shlex.quote(str(python))} -c 'print(123)'"
    if mode == "paths":
        for group in range(3):
            folder = testdir / f"long_fixture_directory_{group}"
            folder.mkdir()
            for n in range(30):
                (folder / f"real_artifact_{n}.txt").touch()
        script = root / "listing.py"
        script.write_text('from pathlib import Path\nfor p in sorted(Path("suite").rglob("*.txt")): print("./" + str(p))\n')
        command = f"{shlex.quote(str(python))} {shlex.quote(str(script))}"
    env = {k: v for k, v in os.environ.items() if not any(w in k.upper() for w in ("TOKEN", "API_KEY", "SECRET", "PROXY"))}
    env.update(HERMES_HOME=str(home), HERMES_RTK_E2E_CORE=str(CORE), PYTHONPATH="/nonexistent/poisoned", RTK_TEST_UNSET="must-be-unset", EXPECTED_PREFIX=str(virtual), SCENARIO=mode)
    if mode in ("missing", "broken", "empty-filter", "bad-filter", "counts", "timeout", "expanded"):
        bindir = root / "bin"
        bindir.mkdir()
        binary = bindir / "rtk"
        if mode != "missing":
            body = {
                "broken": "sys.exit(42)",
                "empty-filter": "pass",
                "bad-filter": "print('No tests collected')",
                "counts": "print('1 passed in 0.1s')",
                "timeout": "time.sleep(3)",
                "expanded": "print(sys.stdin.read() + 'MORE' * 1000)",
            }[mode]
            binary.write_text(f"#!{sys.executable}\nimport sys, time\nfrom pathlib import Path\nPath({str(root / 'rtk-called')!r}).write_text('once')\n{body}\n")
            binary.chmod(0o700)
        env["PATH"] = str(bindir) + os.pathsep + "/usr/bin:/bin"
        if mode != "missing":
            assert shutil.which("rtk", path=env["PATH"]) == str(binary)
        else:
            assert shutil.which("rtk", path=env["PATH"]) is None
    if mode in ("quota", "symlink", "expired"):
        raw = home / "plugin-data/rtk-output/raw"
        raw.parent.mkdir(parents=True)
        if mode == "symlink":
            outside = root / "outside"
            outside.mkdir()
            raw.symlink_to(outside, target_is_directory=True)
        else:
            raw.mkdir()
            if mode == "quota":
                with (raw / "quota.txt").open("wb") as f:
                    f.truncate(64 * 1024 * 1024)
            else:
                expired = raw / "expired.txt"
                expired.write_text("old")
                os.utime(expired, (1, 1))
    proc = subprocess.run([sys.executable, "-I", str(CONSUMER), str(CORE), str(home), command], env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads((home / "result.json").read_text())
    return result, home


@pytest.mark.parametrize("mode", ["normal", "quiet", "script", "expired", "guard-normal"])
def test_successful_native_execution_and_full_redacted_recovery(tmp_path, mode):
    result, home = run_scenario(tmp_path / mode, mode)
    assert result["exit_code"] == 0 and result["error"] is None
    assert "80 passed" in result["output"] and "[RTK: full redacted output:" in result["output"]
    match = re.search(r"full redacted output: (.+)\]", result["output"])
    assert match is not None
    path = Path(match[1])
    original = path.read_text()
    assert "80 passed" in original and "fixture log " in original
    assert SECRET not in original and SECRET not in result["output"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    receipt = json.loads(path.with_suffix(".json").read_text())
    assert receipt["input_bytes"] == len(original.encode())
    assert receipt["shown_bytes"] == len(result["output"].encode())
    assert receipt["shown_bytes"] < receipt["input_bytes"] and receipt["exit_code"] == 0
    assert len((home.parent / "executions").read_text().splitlines()) == 80
    assert not (path.parent / "expired.txt").exists()


@pytest.mark.parametrize("mode,code", [("failure", 1), ("collection", 2), ("empty", 5), ("masked", 0), ("unknown", 0), ("small", 0), ("missing", 0), ("broken", 0), ("empty-filter", 0), ("bad-filter", 0), ("counts", 0), ("timeout", 0), ("expanded", 0), ("quota", 0), ("symlink", 0), ("approval", -1), ("background", 0)])
def test_failures_and_unsupported_results_are_not_filtered(tmp_path, mode, code):
    result, home = run_scenario(tmp_path / mode, mode)
    assert result["exit_code"] == code
    assert "[RTK:" not in result["output"]
    if mode == "failure":
        assert "DIAGNOSTIC_SENTINEL" in result["output"] and "1 failed" in result["output"]
    if mode in ("collection", "masked"):
        assert "SyntaxError" in result["output"]
    if mode == "unknown":
        assert "OPAQUE_SENTINEL" in result["output"]
    if mode == "symlink":
        assert list((home.parent / "outside").iterdir()) == []
    assert list((home / "plugin-data/rtk-output/raw").glob("*.json")) == []
    if mode in ("broken", "empty-filter", "bad-filter", "counts", "timeout", "expanded"):
        assert (home.parent / "rtk-called").read_text() == "once"
    if mode in ("missing", "broken", "empty-filter", "bad-filter", "counts", "timeout", "expanded", "quota", "symlink"):
        assert "80 passed" in result["output"]
        assert len((home.parent / "executions").read_text().splitlines()) == 80
    if mode == "approval":
        assert result["approval_pending"] is True
        assert not (home.parent / "executions").exists()


@pytest.mark.parametrize("enabled", [False, True])
def test_skill_size_flag_survives_real_dispatch_with_rtk(tmp_path, enabled):
    result, home = run_scenario(tmp_path / "composition", "guard-growth", enabled=enabled)
    assert result["exit_code"] == 0 and "80 passed" in result["output"]
    assert result["output"].count("over the limit for SKILL.md") == 1
    assert "IMMEDIATELY" in result["output"] and "[RTK:" not in result["output"]
    assert len((home / "skills/demo/SKILL.md").read_text()) == 7001
    assert len((home.parent / "executions").read_text().splitlines()) == 80


def test_auto_filter_uses_a_real_filesystem_listing(tmp_path):
    result, home = run_scenario(tmp_path / "paths", "paths")
    assert result["exit_code"] == 0 and "[RTK:" in result["output"]
    match = re.search(r"full redacted output: (.+)\]", result["output"])
    assert match is not None
    path = Path(match[1])
    assert len(path.read_text().splitlines()) == 90
    assert json.loads(path.with_suffix(".json").read_text())["filter"] == "auto"
    assert not (home.parent / "executions").exists()


def test_disabled_plugin_is_a_real_negative_control(tmp_path):
    result, home = run_scenario(tmp_path / "disabled", "normal", enabled=False)
    assert result["exit_code"] == 0 and "80 passed" in result["output"]
    assert "fixture log " in result["output"] and "[RTK:" not in result["output"]
    assert not (home / "plugin-data/rtk-output").exists()
