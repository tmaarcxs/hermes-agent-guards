"""Real serve/WebSocket turns, real provider recall, controlled external HTTP services."""
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]
CORE = Path(os.environ.get("HERMES_REVIEWER_E2E_CORE", Path.home() / ".hermes/hermes-agent"))
HINDSIGHT = Path(os.environ.get("HERMES_REVIEWER_E2E_HINDSIGHT", Path.home() / ".hermes/plugins/hindsight"))
FACT_ID = "11111111-1111-4111-8111-111111111111"
USER = "Synthetic Atlas database now runs on port 6543, not 5432. Reply only acknowledged."


@pytest.mark.parametrize("scenario", ["desktop", "baseline", "tui", "tool", "cron", "delegated", "new_input"])
def test_legacy_desktop_at_serve_websocket(scenario, tmp_path):
    if not (CORE / "hermes_cli/main.py").exists() or not HINDSIGHT.exists():
        pytest.skip("Installed Hermes core and Hindsight plugin required")
    home = tmp_path / "home"
    home.mkdir()
    (home / "workspace").mkdir()
    facts = {"id": FACT_ID, "text": "Synthetic Atlas database runs on port 5432.", "type": "world", "state": "valid"}
    calls, patches, reads, frames = [], [], [], []
    stages = []
    proposals = [0]
    proposed = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, body, status=200):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            calls.append(("GET", self.path))
            if self.path.endswith("/memories/" + FACT_ID):
                reads.append(facts["state"])
                self.reply(dict(facts))
            elif self.path.endswith("/models"):
                self.reply({"data": []})
            else:
                self.reply({"version": "0.5.0", "status": "ok"})

        def do_PATCH(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path.endswith("/memories/" + FACT_ID), self.path
            calls.append(("PATCH", self.path))
            patches.append(body)
            facts.update(state=body["state"], invalidation_reason=body["reason"])
            self.reply({})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(("POST", self.path))
            if self.path.endswith("/memories/recall"):
                self.reply({"results": [{"id": FACT_ID, "text": facts["text"], "type": "world"}]})
                return
            if self.path == "/api/show":
                self.reply({"error": "Not an Ollama fixture"}, 404)
                return
            assert self.path.endswith("/chat/completions"), (self.path, body)
            with (tmp_path / "model-requests.jsonl").open("a") as output:
                output.write(json.dumps(body) + "\n")
            fmt = body.get("response_format", {})
            schema = fmt.get("json_schema", {}).get("schema", {})
            messages = body["messages"]
            def text(value):
                return "\n".join(part.get("text", "") for part in value) if isinstance(value, list) else str(value)
            instruction = text(messages[0].get("content", ""))
            if "title" in schema.get("properties", {}):
                content = json.dumps({"title": "Synthetic reviewer regression"})
            elif schema:
                parts = messages[-1]["content"]
                instruction = parts[0]["text"] if isinstance(parts, list) else instruction
                payload = json.loads(parts[-1]["text"] if isinstance(parts, list) else parts)
                if "verdict" in schema.get("properties", {}):
                    stages.append("confirmation")
                    answer = {"verdict": "refutes"}
                elif "NOISE" in instruction:
                    stages.append("noise")
                    answer = {"items": [{"id": row["id"], "decision": "keep", "reason": "none", "duplicate_of": ""} for row in payload["MEMORIES"]]}
                else:
                    stages.append("proposal")
                    proposals[0] += 1
                    proposed.set()
                    if scenario == "new_input" and proposals[0] == 1:
                        assert release.wait(30), "New input not admitted"
                    answer = {"items": [{"id": row["id"], "decision": "keep" if scenario == "new_input" and proposals[0] > 1 else "candidate", "category": "other", "evidence_quote": USER} for row in payload["MEMORIES"]]}
                content = json.dumps(answer)
            else:
                content = "acknowledged"
            if body.get("stream"):
                chunks = [{"id": "synthetic", "object": "chat.completion.chunk", "model": body["model"], "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}]},
                          {"id": "synthetic", "object": "chat.completion.chunk", "model": body["model"], "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
                raw = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            else:
                self.reply({"id": "synthetic", "object": "chat.completion", "created": 0, "model": body["model"], "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:" + str(server.server_port)
    plugin_root = Path(os.environ.get("HERMES_REVIEWER_E2E_ROOT", ROOT))
    category = home / "plugins/hermes-agent-guards"
    category.mkdir(parents=True)
    shutil.copytree(plugin_root / "hindsight-reviewer", category / "hindsight-reviewer", ignore=shutil.ignore_patterns("__pycache__", "tests"))
    if scenario == "baseline":
        target = category / "hindsight-reviewer"
        for name in ("reviewer.py", "plugin.yaml"):
            data = subprocess.check_output(["git", "show", "2c2cda69a18294891bd02e80d50826a3ef5e4ca6:hindsight-reviewer/" + name], cwd=ROOT)
            (target / name).write_bytes(data)
    (home / "plugins/hindsight").symlink_to(HINDSIGHT, target_is_directory=True)
    key = "hermes-agent-guards/hindsight-reviewer"
    provider = "custom:serve-fixture"
    cfg = {"model": {"default": "serve-fixture", "provider": provider},
           "providers": {"serve-fixture": {"base_url": base + "/v1", "api_key": "synthetic", "transport": "chat_completions"}},
           "cron": {"model": "serve-fixture", "model_provider": provider},
           "memory": {"provider": "hindsight"}, "memory_enabled": True,
           "terminal": {"cwd": str(home / "workspace")},
           "agent": {"max_turns": 2}, "platform_toolsets": {"cli": ["terminal"], "desktop": ["terminal"]},
           "plugins": {"enabled": [key, "hindsight"], "auto_apply": False,
                        "entries": {key: {"llm": {"allow_provider_override": True, "allow_model_override": True}}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(cfg))
    (home / ".env").write_text("OPENAI_API_KEY=synthetic\n")
    (home / "hindsight").mkdir()
    (home / "hindsight/config.json").write_text(json.dumps({"mode": "cloud", "api_url": base, "api_key": "synthetic", "bank_id": "synthetic", "recall_sync": True, "auto_retain": False}))
    env = {k: os.environ[k] for k in ("PATH", "LANG", "SSL_CERT_FILE", "SYSTEMROOT") if k in os.environ}
    env.update(HERMES_HOME=str(home), HOME=str(tmp_path), TMPDIR=str(tmp_path), HERMES_DISABLE_LAZY_INSTALLS="1", HERMES_DESKTOP="1", HERMES_DASHBOARD_SESSION_TOKEN="synthetic-loopback-token", HERMES_ACCEPT_HOOKS="1", PYTHONPATH=str(CORE))
    if scenario == "delegated":
        env["HERMES_DELEGATED_CHILD_CONTEXT"] = "true"
    log_path = tmp_path / "serve.log"
    log = log_path.open("w")
    launch = "import sys; sys.path.insert(0, " + repr(str(CORE)) + "); from hermes_cli.main import main; sys.exit(main())"
    proc = subprocess.Popen([sys.executable, "-c", launch, "serve", "--host", "127.0.0.1", "--port", "0", "--skip-build"], stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    try:
        deadline = time.monotonic() + 40
        port = None
        while time.monotonic() < deadline and proc.poll() is None:
            match = re.search(r"HERMES_BACKEND_READY port=(\d+)", log_path.read_text())
            if match:
                port = int(match[1])
                break
            time.sleep(0.1)
        assert port, log_path.read_text()[-8000:]

        async def drive():
            async with connect("ws://127.0.0.1:" + str(port) + "/api/ws?token=synthetic-loopback-token", proxy=None, max_size=8388608) as ws:
                sequence = 0
                async def rpc(method, **params):
                    nonlocal sequence
                    sequence += 1
                    rid = str(sequence)
                    await ws.send(json.dumps({"id": rid, "method": method, "params": params}))
                    while True:
                        frame = json.loads(await asyncio.wait_for(ws.recv(), 45))
                        frames.append(frame)
                        if frame.get("id") == rid:
                            assert "error" not in frame, frame
                            return frame["result"]
                created = await rpc("session.create", source=scenario if scenario in {"tui", "tool", "cron"} else "gui", cwd=str(home / "workspace"), title="Synthetic reviewer regression")
                sid = created["session_id"]
                other = await rpc("session.create", source="gui", cwd=str(home / "workspace"), title="Synthetic unrelated session")
                submitted = await rpc("prompt.submit", session_id=sid, text=USER)
                assert submitted.get("status") == "streaming", submitted
                stop = time.monotonic() + 55
                second = False
                while time.monotonic() < stop:
                    if scenario == "new_input" and proposed.is_set() and not second:
                        await rpc("prompt.submit", session_id=sid, text="A newer input arrived. Reply only acknowledged.")
                        second = True
                        release.set()
                    try:
                        frame = json.loads(await asyncio.wait_for(ws.recv(), 0.2))
                        frames.append(frame)
                    except TimeoutError:
                        pass
                    notices = [f for f in frames if f.get("params", {}).get("type") == "review.summary"]
                    audit = "\n".join(p.read_text() for p in (home / "logs").glob("*.log"))
                    if scenario == "desktop" and notices:
                        break
                    if scenario in {"baseline", "tui", "tool", "cron", "delegated"} and '"reason": "no_origin"' in audit:
                        break
                    if (scenario == "new_input" and second and audit.count(json.dumps({"event": "finished"})[1:-1]) >= 2
                            and sum(f.get("params", {}).get("type") == "message.complete" for f in frames) >= 2):
                        break
                notices = [f["params"] for f in frames if f.get("params", {}).get("type") == "review.summary"]
                assert all(n["session_id"] == sid for n in notices), notices
                assert all(n["session_id"] != other["session_id"] for n in notices)
                assert any(path.endswith("/memories/recall") for _, path in calls), calls
                if scenario == "desktop":
                    assert facts["state"] == "invalidated" and len(patches) == 1, (facts, calls, audit[-9000:])
                    assert stages.count("proposal") == 1 and stages.count("confirmation") == 1, stages
                    assert reads[-1] == "invalidated" and reads.count("valid") >= 2, reads
                    assert len(notices) == 1 and "Memory review:" in notices[0]["payload"]["text"], (notices, audit[-9000:])
                    types = [f.get("params", {}).get("type") for f in frames]
                    assert types.index("message.complete") < types.index("review.summary"), types
                else:
                    assert not patches and not notices and facts["state"] == "valid", (patches, notices, audit[-9000:])
                    if scenario != "new_input":
                        assert "no_origin" in audit, audit[-9000:]
                    if scenario == "new_input":
                        assert second and proposed.is_set() and "superseded" in audit, audit[-9000:]
                with sqlite3.connect(home / "state.db") as db:
                    assert db.execute("SELECT COUNT(*) FROM messages WHERE role = ?", ("user",)).fetchone()[0] >= 1
                print("LEGACY_SERVE_E2E=" + json.dumps({"scenario": scenario, "session_id": sid, "patches": len(patches), "notices": len(notices), "reads": reads, "calls": calls, "serve_log": str(log_path)}))
        asyncio.run(drive())
    finally:
        release.set()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
