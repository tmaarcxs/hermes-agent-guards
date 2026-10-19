import sys
from typing import Any

import pytest

guards = sys.modules["agent_guards"]
gate = guards.background_notify_gate


def call(args: dict[str, Any]) -> Any:
    return gate.check("terminal", args)


@pytest.fixture(autouse=True)
def worker_setup(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "worker")


def test_bounded_job_without_notify_is_blocked():
    result = call({"background": True, "command": "python3 replay.py > replay.log 2>&1"})
    assert result and "agent-guards blocked terminal" in result
    assert "notify=true" in result


def test_notify_true_passes():
    assert call({"background": True, "command": "python3 replay.py", "notify": True}) is None


def test_notify_pattern_list_passes():
    args = {"background": True, "command": "python3 replay.py", "notify": ["Traceback", "EXIT_OK"]}
    assert call(args) is None


def test_empty_notify_list_does_not_count():
    assert call({"background": True, "command": "python3 replay.py", "notify": []})


def test_heartbeat_passes():
    assert call({"background": True, "command": "python3 long.py", "heartbeat": 120}) is None


def test_zero_heartbeat_does_not_count():
    assert call({"background": True, "command": "python3 long.py", "heartbeat": 0})


def test_pty_is_exempt():
    assert call({"background": True, "command": "python3 -i", "pty": True}) is None


def test_foreground_calls_are_not_touched():
    assert call({"command": "python3 replay.py"}) is None


def test_other_tools_are_not_touched():
    assert gate.check("execute_code", {"code": "x"}) is None


@pytest.mark.parametrize(
    "command",
    [
        "python3 -m http.server 8765 --directory /tmp/x",
        "cd /x && python -m http.server 8766 --bind 127.0.0.1",
        "env -u PYTHONPATH /path/.venv/bin/python -m http.server 54337 --bind 127.0.0.1",
        "CI=true npx vite preview --port 4179 --strictPort",
        "CI=true node_modules/.bin/vite preview --host 127.0.0.1 --port 5198 --strictPort",
        "CI=true MSW_ENABLED=false pnpm exec vite --port 18744 --strictPort",
        "pnpm exec vite preview --host 127.0.0.1 --port 4179 --strictPort",
        "uvicorn app.main:app --port 8000",
        "python3 auth_proxy.py 8766 tok.txt",
        "cd /a && python3 auth_proxy.py 8766 tok.txt",
        "cd /a && .venv/bin/python mock_source_server.py > server.log 2>&1",
        "python3 artifact_proxy.py 8766 /tmp/token.txt >>/tmp/artifact_proxy8766.log 2>&1",
        "docker compose up",
        "docker run postgres:16",
        "SCR=/x; mkdir -p $SCR; cd $SCR && python3 -m http.server 8747 --bind 127.0.0.1",
        "python3 -c 'import x' && python3 -m http.server 52979 --bind 127.0.0.1 --directory deliverable",
        "python3 -c \"\nimport http.server, socketserver\nclass H(http.server.SimpleHTTPRequestHandler):\n    def end_headers(self):\n        self.send_header('Connection', 'close')\nsocketserver.TCPServer.allow_reuse_address = True\nwith socketserver.TCPServer(('127.0.0.1', 18719), H) as s:\n    s.serve_forever()\n\"",
    ],
)
def test_server_launchers_are_exempt(command: str):
    assert call({"background": True, "command": command}) is None


@pytest.mark.parametrize(
    "command",
    [
        "python3 replay.py > replay.log 2>&1",
        "pnpm exec vite build --mode development --base / 2>&1 | tail -20",
        "python3 /tmp/build98/metadata_batch.py 2>&1 | tail -20",
        "cd ~/x && python3 replay.py 2>&1 | tee /tmp/o.log",
        "python -m pytest tests/ -q",
        "pnpm install",
        "for i in $(seq 1 15); do sleep 60; done",
        "sleep 240; echo POLL_DUE_8062",
        "echo hello && python3 batch.py && echo done",
        "cd ~/x && git commit --amend --only -m 'msg' 2>&1 | tail -2",
        "nohup ./probe_1.sh > probe_1.log 2>&1 & echo pid=$!",
        "bash launch.sh run_quality.py > run.log 2>&1; echo EXIT=$?; echo done",
        "python3 benchmark_driver.py run --journey j > out.json 2> err; echo RUN_EXIT=$?",
        "cd /w/t && env -i PATH=/bin:/usr/bin SNOWFLAKE_USER=M /usr/bin/python3 sf.py",
        "python3 -c \"import json, socketserver; json.load(open('a.json')); print(1)\"",
    ],
)
def test_bounded_jobs_are_still_blocked(command: str):
    assert call({"background": True, "command": command})


def test_not_a_worker_is_exempt(monkeypatch):
    monkeypatch.setattr(guards.kanban_policy, "configured_worker", lambda: "")
    assert call({"background": True, "command": "python3 replay.py"}) is None


def test_message_carries_the_fix():
    result = call({"background": True, "command": "python3 replay.py"})
    assert "notify=true" in result


def test_block_message_is_compact_and_names_the_fix():
    result = call({"background": True, "command": "python3 replay.py"})
    assert len(result.encode("utf-8")) <= 320
    assert "background=true" in result
    assert "notify=true" in result
