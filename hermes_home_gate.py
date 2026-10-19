"""Recognize and block terminal commands that recursively delete a live Hermes home.

Split from ``__init__.py`` (PR-N): behaviour-identical move of the
``hermes_home`` guard. Blocks ``rm -r``/``-R``/``--recursive``, ``find``
``-delete``/``-exec rm -r...``, ``rsync --delete`` (destination side only) and
``shutil.rmtree``/``os.remove``/``os.unlink``/``os.rmdir``/``pathlib`` rmdir/unlink
calls inside ``python -c`` payloads and python heredoc bodies, aimed at
``$HERMES_HOME``, ``~/.hermes`` / ``$HOME/.hermes`` (literal absolute forms for any
user included), ``~/.hermes/profiles`` or any single profile home, an ancestor of
these (``~``, ``$HOME``, ``/home/<user>``, ``/Users/<user>``, ``/root``, ``/``), or
a stage reached through a runner chain (``sudo``, ``env``, ``nohup``, ``nice``,
``exec``, ``time``, ``parallel``, ``command``, ``xargs`` with its flags) after a
pipe. Quoted prose and heredoc bodies stay exempt (single quotes especially do not
contribute pipe stages), deeper nests below an ancestor stay allowed when they
exclude the home, and a ``find -name`` filter narrows an ancestor walk below it.
The entire composed-runner surface (N12b) is kept.
"""

import re
import shlex
from typing import Optional

_START = r"(?:^|[;&|\n(`{]|\$\(|\bthen\b|\bdo\b|\belse\b)\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:sudo\s+|command\s+)?"
_SEGMENT_END = re.compile(r"[;\n]|&&|\|\|")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w[\w.]*)\1")
_RECURSIVE_RM_FLAGS = re.compile(r"(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b")
_RECURSIVE_RM = re.compile(_START + r"rm\b(?=[^;&|\n]*" + _RECURSIVE_RM_FLAGS.pattern + r")")


_FIND_DELETE = re.compile(_START + r"find\b")
_FIND_DELETE_FLAGS = re.compile(r"\s-delete\b")
_FIND_NAME_FILTER = re.compile(r"-name\b")
_FIND_EXEC_RM = re.compile(r"-exec\s+(?:sudo\s+)?rm\s(?:[^;{}])*-[a-zA-Z]*r")
_RSYNC_DELETE = re.compile(_START + r"rsync\b")
_RSYNC_DELETE_FLAG = re.compile(r"(?:^|[\s/])--delete(?=[\s'=\"]|$)")
_PYTH_HERMES_ENV_RE = re.compile(r"os\.environ(?:\.get)?\[?\s*\(??\s*['\"]([^'\"]+)['\"]")
_HERMES_HOME_DELETE_MESSAGE = (
    "hermes_home_guard: this would recursively delete a live Hermes home. For tests, create a throwaway "
    "home with `mktemp -d` inside the scratch workspace and run the command as "
    '`env HERMES_HOME="$(mktemp -d "$PWD/.hermes-test-home.XXXXXX")" <test cmd>`; '
    "delete the printed directory by its literal path."
)
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600}


def _strip_heredocs(command: str) -> str:
    """Blank out heredoc bodies: a script's own `sleep` (bash or `time.sleep`
    in Python) is not the agent blocking, and scripting text that merely says
    `sleep 600` must not be read as a command (measured: 3 false blocks)."""
    delim = None
    kept = []
    for line in command.split("\n"):
        if delim is not None:
            if line.strip() == delim:
                delim = None
            kept.append("")
            continue
        kept.append(line)
        match = _HEREDOC.search(line)
        if match:
            delim = match.group(2)
    return "\n".join(kept)


_PROTECTED_LEVELS = ("home", "ancestor", "root")


def _level_below_anchor(rest: str) -> str:
    """Classify the path that follows an anchor (a home root or `HERMES_HOME`).

    `.hermes` itself and `.hermes/profiles` (`/profile-name`) are the live
    home; `profiles/<name>/cache/...` and any other child are nests below."""
    if rest in ("", "/"):
        return "home"
    parts = rest.rstrip("/").split("/")
    if parts[0] != ".hermes":
        return "none"
    if len(parts) == 1:
        return "home"
    if parts[1] == "profiles" and len(parts) in (2, 3):
        return "home"
    return "below"


def _path_level(token: str) -> str:
    """Map one shell/Python path token to its protection level (`home`,
    `ancestor`, `root`, `below`, `none`). `~<user>` stays out of scope: `~u`
    never maps onto `/home/u` without knowing the OS."""
    t = token.strip().strip('"').strip("'")
    if t.startswith("~"):
        body = t[1:]
        user = body.split("/", 1)[0]
        if not user:
            if body in ("", "/"):
                return "ancestor"
            return _level_below_anchor(body[1:] if body.startswith("/") else body)
        return "none"
    if t in ("$HOME", "${HOME}", "$HOME/", "${HOME}/"):
        return "ancestor"
    if t.startswith("$HOME"):
        rest = t[len("$HOME"):]
        if rest in ("", "/"):
            return "ancestor"
        if rest.startswith("/"):
            return _level_below_anchor(rest[1:])
        return "none"
    if t in ("$HERMES_HOME", "${HERMES_HOME}", "$HERMES_HOME/", "${HERMES_HOME}/"):
        return "home"
    if t.startswith("$HERMES_HOME"):
        rest = t[len("$HERMES_HOME"):]
        if rest in ("", "/"):
            return "home"
        if rest.startswith("/"):
            return _level_below_anchor(rest[1:])
        return "none"
    if t.startswith("/root"):
        rest = t[5:]
        if rest in ("", "/"):
            return "ancestor"
        if rest.startswith("/"):
            return _level_below_anchor(rest[1:])
        return "none"
    for prefix in ("/home/", "/Users/"):
        if t.startswith(prefix):
            rest = t[len(prefix):]
            user = rest.split("/", 1)[0]
            if not user:
                return "none"
            rest = rest[len(user):]
            if rest in ("", "/"):
                return "ancestor"
            return _level_below_anchor(rest[1:] if rest.startswith("/") else rest)
    if t == "/":
        return "root"
    return "none"


def _segment_operand_levels(segment: str) -> list[str]:
    """Protection levels of the segment's operand tokens (shlex view)."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    return [level for token in tokens for level in (_path_level(token),) if level != "none"]


def _segment_tokens(segment: str) -> list[str]:
    """shlex token view of one segment with `punctuation_chars=True`: a pipe
    comes back as its own token while quoted payloads stay whole, so a
    quoted `|` can never split the operand side off a deletion verb.
    Unbalanced quotes return no tokens instead of a word-split fallback —
    a `str.split` rebuilds the very pipe/quote confusion (R1) this view
    exists to avoid."""
    lexer = shlex.shlex(segment, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return []


def _pipe_left_tokens(segment: str) -> list[str]:
    """Operand tokens piped into a deletion verb from an earlier pipe stage
    of the same segment (`echo <home> | xargs rm -rf`): every token before
    the LAST `|` token (`||` parses as one token, so it never splits left
    from right)."""
    tokens = _segment_tokens(segment)
    if "|" not in tokens:
        return []
    pipe_index = len(tokens) - 1 - tokens[::-1].index("|")
    return tokens[:pipe_index]


_RUNNER_WORDS = ("sudo", "command", "env", "nohup", "nice", "exec", "time", "parallel")
_RUNNER_XARGS_FLAGS = re.compile(r"xargs(?:\s+-\S+)*")


def _composed_runner_levels(segment: str) -> Optional[set]:
    """Levels a runner-composed deletion would delete (`echo <home> | xargs
    rm -rf`), or `None` when this segment is not one. The runner chain
    (`sudo`, `env`, `nohup`, `nice`, `exec`,
    `time`, `parallel`, `xargs` with its flags) appends the piped-in
    operands to the verb, so the composed command's targets are the verb's
    own operands plus everything left of the last pipe. Semantics stay
    verb-local (a filtered `find` below an ancestor walks only matches; an
    rsync deletes on its destination), mirroring the direct-shape checks."""
    verbs = ("rm", "find", "rsync")
    tokens = _segment_tokens(segment)
    verb_index = None
    for index in range(len(tokens) - 1, -1, -1):
        if tokens[index] in verbs:
            verb_index = index
            break
    if verb_index is None:
        return None
    if (
        tokens[verb_index] == "rm"
        and verb_index >= 2
        and tokens[verb_index - 1] == "-exec"
        and tokens[verb_index - 2] == "find"
    ):
        verb_index -= 2
    runner_seen = False
    for token in reversed(tokens[:verb_index]):
        if token == "|":
            break
        if token in _RUNNER_WORDS or token == "--" or (token.startswith("-") and token != "--") or _RUNNER_XARGS_FLAGS.fullmatch(token):
            runner_seen = True
            continue
        if verb_index + 1 < len(tokens) and tokens[verb_index + 1].startswith("-"):
            break
        if (
            '"' in token or "'" in token or "=" in token
            or token.endswith(",") or token.endswith("[") or token.endswith("]")
        ):
            break
        return None
    if not runner_seen:
        return None
    if verb_index + 1 >= len(tokens) or not tokens[verb_index + 1].startswith("-"):
        return None
    stage = tokens[verb_index:]
    if any(('"' in token and token != '"') for token in stage[2:]):
        return None
    stage_text = " ".join(stage)
    stage_levels = set(_segment_operand_levels(stage_text))
    left_levels = set(
        level for token in tokens[:verb_index] for level in (_path_level(token),) if level != "none"
    )
    levels = stage_levels | left_levels
    if stage[0] == "find":
        if not (_FIND_DELETE_FLAGS.search(stage_text) or _FIND_EXEC_RM.search(stage_text)):
            return None
        if "home" in (levels & set(_PROTECTED_LEVELS) | stage_levels):
            return levels
        if _FIND_NAME_FILTER.search(stage_text):
            return None
        return levels if levels & set(_PROTECTED_LEVELS) else None
    if stage[0] == "rsync":
        if not _RSYNC_DELETE_FLAG.search(stage_text):
            return None
        operands = [token for token in stage[1:] if not token.startswith("-")]
        if operands and _path_level(operands[-1]) in _PROTECTED_LEVELS:
            return levels
        if left_levels & set(_PROTECTED_LEVELS):
            return levels
        return None
    if not _RECURSIVE_RM_FLAGS.search(stage_text):
        return None
    return levels if levels & set(_PROTECTED_LEVELS) else None


def _rsync_delete_target_protected(segment: str) -> bool:
    """True when the `--delete` sync's DESTINATION (last non-option operand,
    the side that loses files) is a protected target; a home as SOURCE syncs
    out to a safe copy and stays allowed."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    operands = []
    for token in tokens[1:]:
        if token.startswith("-"):
            if token in ("--",):
                continue
            continue
        operands.append(token)
    if not operands:
        return False
    return _path_level(operands[-1]) in _PROTECTED_LEVELS


def hermes_home_violations(command: str) -> list[str]:
    """Recursive deletion of the live Hermes home: `$HERMES_HOME`, `~/.hermes`
    (`$HOME/.hermes`, literal absolute forms for any user) and its `profiles`
    subtree itself, an ancestor of it (`~`, `$HOME`, `/home/<user>`,
    `/Users/<user>`, `/root`, `/`), not nests below an ancestor that do not
    include the home. `rm -r/-R/--recursive`, `find -delete`,
    `rsync --delete` (destination side only) plus `shutil.rmtree` inside
    `python -c`/heredocs; single-quoted mention and heredoc prose are exempt,
    while double-quoted content expands and stays visible."""
    found = []
    blanked = _strip_heredocs(command)
    jointed = blanked.replace("\\\n", "")
    for phrase in _python_delete_phrases(command, jointed):
        if _python_rmtree_protected(phrase):
            found.append(_HERMES_HOME_DELETE_MESSAGE)
            return found
    for segment in _SEGMENT_END.split(jointed):
        composed = _composed_runner_levels(segment)
        if _RECURSIVE_RM.search(segment):
            levels = set(_segment_operand_levels(segment))
        elif composed is not None:
            levels = composed
        else:
            levels = set()
        protected = bool(levels & set(_PROTECTED_LEVELS))
        if _RECURSIVE_RM.search(segment) and protected:
            found.append(_HERMES_HOME_DELETE_MESSAGE)
            continue
        if composed is not None:
            found.append(_HERMES_HOME_DELETE_MESSAGE)
            continue
        left_levels = set(
            level for token in _pipe_left_tokens(segment) for level in (_path_level(token),) if level != "none"
        )
        if _FIND_DELETE.search(segment) and _find_delete_activates(segment, left_levels):
            found.append(_HERMES_HOME_DELETE_MESSAGE)
            continue
        if _RSYNC_DELETE.search(segment) and _RSYNC_DELETE_FLAG.search(segment):
            if _rsync_delete_target_protected(segment):
                found.append(_HERMES_HOME_DELETE_MESSAGE)
    return found


_PY_CODE_ARG = re.compile(r"(?:^|[;&|\n(`{]|\$\(|\bthen\b|\bdo\b|\belse\b)\s*python\d?(?:\d\.\d+)?\s+(?:-[^\s-]\S*\s+)*-c\s*")
_PY_LOADER_LINE = re.compile(r"^\s*(?:sudo\s+)?(?:env\s+\S+=\S+\s+)*python\d?(?:\d\.\d+)?(?:\s|$)")


def _python_delete_phrases(command: str, blanked: str) -> list[str]:
    """`rmtree(...)` occurrences in real python execution contexts: the
    `python -c <code>` payload captured THROUGH its closing quote, and the
    body of a python heredoc read from the original command (the blanked
    text no longer carries bodies). Prose in echo/grep arguments never
    enters those contexts and is skipped."""
    phrases = []
    for match in _PY_CODE_ARG.finditer(blanked):
        phrases.append(_capture_code_payload(blanked, match.end()))
    if _HEREDOC.search(command):
        body = _heredoc_python_body(command)
        if body and _PY_LOADER_LINE.match(command.strip()):
            phrases.append(body)
    return phrases


def _capture_code_payload(text: str, start: int) -> str:
    if start < len(text):
        quote = text[start] if text[start] in ("'", '"') else None
        if quote:
            end_quote = text.find(quote, start + 1)
            while end_quote > 0 and quote == '"' and text[end_quote - 1] == "\\":
                end_quote = text.find(quote, end_quote + 1)
            if end_quote > 0:
                return text[start + 1 : end_quote]
            return text[start + 1 :]
    end = _SEGMENT_END.search(text, start)
    return text[start : end.start() if end else len(text)]


def _heredoc_python_body(command: str) -> Optional[str]:
    lines = command.split("\n")
    delim = None
    collecting = False
    body = []
    for line in lines:
        if delim is not None:
            if line.strip() == delim:
                delim = None
                collecting = False
                continue
            if collecting:
                body.append(line)
            continue
        match = _HEREDOC.search(line)
        if match:
            delim = match.group(2)
            collecting = True
    return "\n".join(body) if body else None


_PY_DELETE_CALL = re.compile(r"\b(?:shutil\.rmtree|os\.(?:remove|unlink|rmdir))\s*\(\s*([^),]+)")
_PY_PATH_DELETE = re.compile(r"\bpathlib\.Path\s*\(\s*(.+?)\s*\)\s*\.\s*(?:rmdir|unlink)\s*\(")
_PY_STRING_BODY = re.compile(r"['\"][^'\"]*['\"]")


def _py_delete_token(raw: str) -> str:
    unescaped = raw.replace('\\"', '"').replace("\\'", "'")
    match = _PY_STRING_BODY.search(unescaped)
    if match:
        literal = match.group(0)
        return literal[1:-1] if len(literal) >= 2 else literal
    return unescaped.strip().strip('"\'')


def _python_rmtree_protected(phrase: str) -> bool:
    """`rmtree`, `os.remove`/`unlink`/`rmdir` and `pathlib.Path(...).rmdir()/.unlink()` arguments
    resolve to one token each; `\"` inside a doubly-quoted `-c` payload is un-escaped before the
    string literal is isolated, so a backslash never reads as the delimiter that ends the
    argument (S1)."""
    calls = []
    for match in _PY_DELETE_CALL.finditer(phrase):
        calls.append(match.group(1))
    for match in _PY_PATH_DELETE.finditer(phrase):
        calls.append(match.group(1))
    for raw in calls:
        token = raw.strip()
        if not ("os.environ" in token or "os.getenv" in token):
            token = _py_delete_token(token)
        if "os.environ" in token or "os.getenv" in token:
            literal = _PYTH_HERMES_ENV_RE.search(token)
            if literal and "HERMES_HOME" in literal.group(1):
                return True
            continue
        if _path_level(token) in _PROTECTED_LEVELS:
            return True
    return False


def _find_delete_activates(segment: str, left_levels: Optional[set] = None) -> bool:
    """`find <operand>` deletes recursively with `-delete` or
    `-exec rm -r...`; a home operand always blocks, an ancestor or root
    operand only when no `-name` filter narrows the walk below it, a nest
    below never blocks."""
    levels = set(_segment_operand_levels(segment)) | set(left_levels or ())
    if not any(level in _PROTECTED_LEVELS for level in levels):
        return False
    if not (_FIND_DELETE_FLAGS.search(segment) or _FIND_EXEC_RM.search(segment)):
        return False
    if "home" in levels:
        return True
    return _FIND_NAME_FILTER.search(segment) is None
