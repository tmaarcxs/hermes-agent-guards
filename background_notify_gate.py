"""Require an outcome channel on bounded background terminal jobs.

A ``terminal`` call with ``background=true`` that is **not** a long-lived server must
say how its completion reaches the agent: ``notify=true``, a non-empty ``notify``
pattern list, or a positive ``heartbeat`` (seconds). Silent background jobs are how a
run ends up waiting on an outcome that never arrives: the completion event is the
agent's only signal, a ``process_manage`` poll reply does not carry the job's output,
and a dispatcher reclaim on the silent stall re-queues the whole card. ``notify=true``
costs one notification; ``heartbeat`` also keeps the run visibly alive on long jobs.

Exempt:
- ``pty=true`` interactive sessions (driven via ``process_manage``; a completion
  notice does not apply);
- long-lived servers/daemons, detected from observed launch shapes: the **final
  statement** of the command launches a known server (vite dev/preview, http.server,
  uvicorn, gunicorn, local proxies and mock servers, databases, containers) and is
  not piped away. Naming them keeps the list conservative: a bounded job cannot pass
  as an unknown new "server" by renaming; the end-of-command anchor is what separates
  ``python3 -m http.server 8971 --directory X`` (daemon) from
  ``pnpm exec vite build ... 2>&1 | tail -f log`` (bounded, must notify).

Fails open.
"""

import re
from typing import Any, Optional

from . import kanban_policy

MESSAGE = (
    "agent-guards blocked terminal: a background job must say how its completion reaches you. "
    "Add notify=true (one completion notice with the tail of the output) or set heartbeat>0 "
    "for live progress ticks. If this really is a long-lived server/daemon that never completes "
    "(vite, uvicorn, http.server, postgres, docker compose up, ...), it does not need notify; "
    "otherwise a silent background job is how a run ends up waiting on an outcome that never "
    "arrives: add notify, or run it foreground if it is bounded."
)

_RETRY_INSTRUCTION = getattr(kanban_policy, "RETRY_INSTRUCTION", "")

# Statement start: after ;, &, |, ||, &&, newline, (, {, $( then/do/else.
_STMT = re.compile(r"(?:^|[;&|\n(`{]|\$\(|\bthen\b|\bdo\b|\belse\b)[ \t]*")

# A server embedded in an inline `python3 -c "...serve_forever()"` script: the
# launcher is inside Python code, so match the code shape instead of a command name.
_PY_SERVER_CODE = re.compile(r"(?:http\.server|socketserver|serve_forever\(\)|HTTPServer|TCPServer|UnixHTTPServer)")

# Leading env-assignment / wrapper tokens that may precede the launcher.
_LEAD = re.compile(
    r"(?:(?:[A-Za-z_][A-Za-z0-9_]*=\S*|env(?:\s+[-\w]+(?:=?\S*)?)*|sudo|command|exec)\s+)*"
)
# Optional interpreter between the env prefix and the server script/module.
_INTERP = r"(?:[A-Za-z0-9_/.~-]*python[0-9.]*|node|nodejs|bash|sh|zsh)\s+"

_SERVER = re.compile(
    "|".join(
        (
            # JS dev/preview servers: vite/next/nuxt/astro/ng/webpack, with or
            # without a package-manager runner or node_modules/.bin path prefix.
            r"(?:\.{1,2}/)?(?:node_modules/\.bin/)?"
            r"(?:(?:npx|pnpm(?:\s+exec)?|npm(?:\s+run)?|yarn(?:\s+run)?)\s+)?"
            r"(?:" + r"|".join(
                (
                    r"vite\b(?![^\n;&|]*\s(?:build|optimize))",
                    r"ng\s+serve\b",
                    r"next\s+(?:dev|start)\b",
                    r"nuxt\s+dev\b",
                    r"astro\s+(?:dev|preview)\b",
                    r"webpack-dev-server\b",
                )
            ) + r")",
            # Python servers / proxies / mock launchers seen in real background
            # launches, matched by ROLE SUFFIX so any setup's helper scripts are
            # covered without hard-coding project names.
            r"(?:" + _INTERP + r")?-m\s+http\.server\b",
            r"(?:" + _INTERP + r")?(?:uvicorn|gunicorn)\b",
            r"(?:" + _INTERP + r")?flask\s+run\b",
            r"(?:" + _INTERP + r")?streamlit\s+run\b",
            r"(?:" + _INTERP + r")?[A-Za-z0-9_./~-]*(?:_proxy|_server|_mock)\.py\b",
            # Database and infra daemons.
            r"(?:mysqld|redis-server|memcached|mongod|rabbitmq-server)\b",
            # Containers left in the foreground (compose without -d / run without -d).
            r"docker(?:\s|-)compose\s+up\b(?![^\n;&|]*\s(?:-d|--detach)\b)",
            r"docker\s+run\b(?![^\n;&|]*\s(?:-d|--detach)\b)",
        )
    )
)

_TERMINATOR = re.compile(r"[;|\n`]|&&")
_REDIRECT = re.compile(r"\s*\d*>>?\s*\S*(?:\s*2>&1)*")


def _final_statement_is_server(command: str) -> bool:
    """True when the command's last running element is a server/daemon launcher."""
    if not command:
        return False
    if "-c " in command and _PY_SERVER_CODE.search(command):
        # Inline `python3 -c` script that serves forever, or a one-shot py that
        # merely imports the module: anchor on serve_forever/HTTPServer usage.
        stripped = re.sub(r"^import\s+[^\n]+", "", command, flags=re.MULTILINE)
        if re.search(r"\.serve_forever\(\)|HTTPServer|TCPServer|http\.server\.[A-Z]", stripped):
            return True
    found = None
    for match in _STMT.finditer(command):
        tail = _LEAD.sub("", command[match.end():], count=1)
        server_match = _SERVER.match(tail)
        if server_match:
            found = (server_match, tail)
    if found is None:
        return False
    server_match, tail = found
    rest = tail[server_match.end():]
    if re.match(r"\s*\|", rest):
        return False  # piped away: bounded pipeline
    if _REDIRECT.match(rest):
        rest = _REDIRECT.sub("", rest, count=1)
    return not _TERMINATOR.search(rest)


def check(tool_name: str, args: Any) -> Optional[str]:
    if tool_name != "terminal" or not isinstance(args, dict) or not args.get("background"):
        return None
    if args.get("pty"):
        return None
    notify = args.get("notify")
    has_notify = notify is True or (isinstance(notify, (list, tuple)) and len(notify) > 0)
    heartbeat = args.get("heartbeat")
    if has_notify or (isinstance(heartbeat, (int, float)) and heartbeat > 0):
        return None
    if _final_statement_is_server(str(args.get("command") or "")):
        return None
    if not kanban_policy.configured_worker():
        return None
    return MESSAGE + (" " + _RETRY_INSTRUCTION if _RETRY_INSTRUCTION else "")
