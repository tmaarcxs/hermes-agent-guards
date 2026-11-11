"""Real Hermes terminal consumer used by the process-level E2E scenarios."""

import json
import os
from pathlib import Path
import sys

core, home, command = sys.argv[1:4]
setattr(sys, "_hermes_relaunched", True)
sys.path.insert(0, core)
os.environ["HERMES_HOME"] = home
os.environ["TERMINAL_ENV"] = "local"
os.environ["TERMINAL_CWD"] = str(Path(home).parent)
os.environ["HERMES_NONINTERACTIVE"] = "1"

import hermes_cli.plugins as plugins
from model_tools import handle_function_call
from tools.terminal_tool import cleanup_all_environments

plugins.discover_plugins(force=True)
try:
    args = {"command": command, "workdir": str(Path(home).parent), "timeout": 30}
    if os.environ.get("SCENARIO") == "background":
        args.update(background=True, notify=True)
    result = json.loads(handle_function_call("terminal", args, tool_call_id="rtk-fixture-call", session_id="rtk-fixture-session", turn_id="rtk-fixture-turn"))
    if os.environ.get("SCENARIO") == "background":
        result = json.loads(handle_function_call("process_manage", {"action": "wait", "session_id": result["session_id"], "timeout": 30}, tool_call_id="rtk-fixture-wait", session_id="rtk-fixture-session", turn_id="rtk-fixture-turn"))
    Path(home, "result.json").write_text(json.dumps(result))
finally:
    cleanup_all_environments()
