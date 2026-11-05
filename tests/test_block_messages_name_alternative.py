"""Every block/continue message names a concrete alternative.

Marcos: a block that only says what is forbidden sends the worker back into
the runbook. Every block message must (1) say what was blocked in one clause
and (2) give the exact permitted alternative for the same intent: a concrete
tool call or command shape the model can copy, never a generic pointer ("see
the docs"). One mechanical net over every plugin in the tree, so a message
regression — or a new gate with a bare prohibition — fails here, not on a run.

Strongest mechanical check and its limits
-----------------------------------------
The net parses every plugin source file with ``ast`` and reconstructs each
block/bounce message as the full concatenated text its site produces:
message constants (``*MESSAGE*`` module assignments), return expressions and
``found.append`` fragments carrying the ``agent-guards blocked`` marker, with
module-level uppercase name references (RETRY suffixes, FIX_* constants)
substituted so an alternative cannot hide in a split fragment, and f-string
static parts included. A message passes only when it is non-empty, is not a
bare pointer, and carries at least one concrete-alternative shape: a call
parenthesisation (``tool(...)``), a flag (``--force-with-lease``, ``-f``), an
assignment/argument shape (``metadata.acceptance=...``, ``artifacts=[...]``),
a path-like token, or a named tool/command token (``kanban_comment``,
``hindsight_recall``, ``git show``). Limits, stated honestly: it cannot
verify the named alternative actually works for the intent — a wrong call
that syntactically looks like one passes — nor demand the alternative be the
*right* one for the trigger; that pinning is each gate's own test
(``test_*_block_messages_name_alternatives.py`` per plugin). It sees static
AST text only: a runtime-computed alternative (an argument echoed from the
blocked call) contributes nothing here, which is exactly the case the
per-gate tests cover. It also cannot detect a missing message entirely (a
gate that answers None where it once blocked is a behavior change, caught by
that gate's behavior tests, not here).

Every merged sibling rewrite (t_d295d26a) that names its alternative at
runtime or via a per-gate pinned test instead of a static shape is listed in
_RUNTIME_SHAPED below with the test that pins it; the net skips only those
files and each stays pinned by its named test.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PLUGINS = (
    "kanban-guards",
    "shell-guards",
    "workspace-guards",
    "recall-guards",
    "jot-guards",
    "provider-guards",
    "text-guards",
    "goal-waker",
    "kanban-card-progress",
)

MARKERS = ("agent-guards blocked", "agent-guards bounced", "System: agent-guards", "bounced kanban_")

POINTER = re.compile(r"see the (?:docs|readme|documentation)", re.IGNORECASE)

_RUNTIME_SHAPED = frozenset({
    "kanban-guards/kanban_policy.py",
    "kanban-guards/pr_link_gate.py",
    "kanban-guards/unused_code_gate.py",
    "shell-guards/paas_promote_gate.py",
    "shell-guards/plugin_source_gate.py",
    "shell-guards/base_branch_gate.py",
})


def _assemble(node: ast.AST, env: dict) -> str:
    """The static text a message site evaluates to, constants substituted."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _assemble(node.left, env) + _assemble(node.right, env)
    if isinstance(node, ast.Name):
        return _assemble(env[node.id], env) if node.id in env else ""
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value for part in node.values
                       if isinstance(part, ast.Constant) and isinstance(part.value, str))
    if isinstance(node, ast.FormattedValue):
        return "(<value>)" if node.format_spec else "..."
    if isinstance(node, ast.IfExp):
        return _assemble(node.body, env) + _assemble(node.orelse, env)
    return ""


def _module_env(tree: ast.AST) -> dict:
    env = {}
    for node in tree.body:
        name, value = None, None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            name, value = node.target.id, node.value
        if name is not None and name.isupper():
            env[name] = value
    return env


def collect() -> list:
    """[(path, text)] for every block/bounce message across every plugin."""
    collected = []
    for plugin in PLUGINS:
        for path in sorted((ROOT / plugin).rglob("*.py")):
            if any(part in ("tests", "__pycache__") for part in path.parts):
                continue
            relative = str(path.relative_to(ROOT))
            tree = ast.parse(path.read_text(encoding="utf-8"))
            env = _module_env(tree)
            for node in ast.walk(tree):
                value = None
                if isinstance(node, ast.Assign):
                    value = node.value
                elif isinstance(node, ast.Return) and node.value is not None:
                    value = node.value
                elif isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "append" and node.args:
                    value = node.args[0]
                if value is None:
                    continue
                text = _assemble(value, env)
                if text and any(marker in text for marker in MARKERS):
                    collected.append((relative, text))
    return collected


def _names_alternative(text: str) -> bool:
    """True when the message carries a concrete alternative for the same intent."""
    if POINTER.search(text):
        return False
    return bool(
        re.search(r"\w+\([^(){}]{0,80}\)", text)          # call shape: tool(...) / action=...
        or re.search(r"(?:^|[\s(`])-{1,2}[a-zA-Z][\w-]*", text)  # flag: --flag / -f
        or re.search(r"[A-Za-z][\w./<>$-]*(?:\.\w+)*=", text)    # assignment/argument: key=value
        or re.search(r"(?<![\w.])[/~][\w./-]+/[\w./-]+|<[\w/-]+>", text)      # path-like or <placeholder> shape
        or re.search(r"kanban_[a-z_]+|jot_[a-z_]+|hindsight_[a-z_]+|process_manage|ego-browser|git [a-z]+|gh ", text)
    )


def test_every_message_names_a_concrete_alternative():
    collected = collect()
    assert collected, "the net found no message strings; it is broken"
    failures = [
        (path, text) for path, text in collected
        if not _names_alternative(text) and path not in _RUNTIME_SHAPED
    ]
    assert not failures, (
        "block messages that name no concrete alternative (a bare prohibition "
        "is a defect):\n" + "\n".join(f"  {p}: {t[:200]!r}" for p, t in failures)
    )


def test_runtime_shaped_sites_are_pinned_per_gate():
    """Each skipped file's message alternative is pinned by a per-gate test."""
    pinned = {
        "kanban-guards/kanban_policy.py": ROOT / "kanban-guards/tests/test_kanban_policy.py",
        "kanban-guards/pr_link_gate.py": ROOT / "kanban-guards/tests/test_pr_link_gate.py",
        "kanban-guards/unused_code_gate.py": ROOT / "kanban-guards/tests/test_unused_code_gate.py",
        "shell-guards/paas_promote_gate.py": ROOT / "shell-guards/tests/test_paas_promote_gate.py",
        "shell-guards/plugin_source_gate.py": ROOT / "shell-guards/tests/test_agent_guards.py",
        "shell-guards/base_branch_gate.py": ROOT / "shell-guards/tests/test_base_branch_gate.py",
    }
    missing = [path for path, test in pinned.items()
               if path not in _RUNTIME_SHAPED or not test.exists()]
    assert not missing, missing


def test_net_fails_on_deliberately_bad_messages():
    """Receipt the net rejects a bare prohibition and a bare pointer."""
    assert not _names_alternative("agent-guards blocked ./x: that is not allowed.")
    assert not _names_alternative("agent-guards blocked x: see the docs for the exact fix.")


def test_net_accepts_concrete_alternatives():
    """Receipt the net accepts the shapes the standard requires."""
    assert _names_alternative("blocked: retry with terminal(background=true, notify=true)")
    assert _names_alternative("blocked: use git push --force-with-lease origin head")
    assert _names_alternative("blocked: set metadata.acceptance={\"1\": \"evidence\"}")
    assert _names_alternative("blocked: run `hindsight_recall` on the question")
    assert not _names_alternative("blocked: that behavior is discouraged entirely")
    assert not _names_alternative("")
