"""Block edits to installed plugin checkouts and route them to the working clone.

The installed copies of our plugins live under ``~/.hermes/plugins/<name>/`` and
``~/.hermes/profiles/<profile>/plugins/<name>/``, each one a git checkout with an
origin remote. They are runtime copies: the change belongs in the working clone
under ``~/git_repos``, pushed, then synced with
``hermes -p <profile> plugins update <name>``. ``plugin_source_gate`` only
refuses an activation whose tree is already dirty, so nothing stopped the edit
itself — twice corrected by hand (2026-11-01, 2026-11-05).

Detection is dynamic, never a plugin-name list: the write target resolves under
a ``plugins`` directory of ``~/.hermes`` or of a profile, and the nearest
enclosing git work tree carries an ``origin`` remote. A plugin directory that is
not a git checkout (locally developed plugin) stays writable. Reads, ``hermes
... plugins update|install|enable|disable|list|doctor``, read-only git
inspections (``fetch``, ``status``, ``pull --ff-only``, ``rebase origin/...``)
and writes inside a worktree of the working clone all pass. Every internal
error, missing git binary and git timeout fails open.
"""

import re
import subprocess
from pathlib import Path
from typing import Any, List, Optional

from . import _paths, home_litter_gate

_GIT_TIMEOUT = 10

_SAFE_GIT = frozenset({"fetch", "status", "log", "diff", "show", "rev-parse", "remote", "branch", "ls-files", "describe", "blame"})
"""Git subcommands that never change the checked-out tree."""

_MUTATING_GIT_HEADS = frozenset({"checkout", "restore", "apply"})
"""Git subcommands the task names as writes when the repository side lives in an installed checkout."""

_LITERAL_HEADS = frozenset({"python", "python3", "perl"})
"""Heads whose string literals can carry an inline write target."""

_COPY_HEADS = frozenset({"cp", "mv", "rsync", "install"})
"""Copy-style heads: only the LAST operand is written; earlier ones are sources."""
_TARGET_HEADS = frozenset({"touch", "mkdir", "rm"})
"""Heads that create/touch/remove every path operand they name."""

_IN_PLACE = re.compile(r"(?:^|\s)-i(?:\S*\s|\s)")
"""GNU/BSD in-place marker for ``sed`` and ``perl`` (``-i``, ``-i.bak``, ``-i ''``)."""

_PERL_OPEN = re.compile(r"\bopen\s*\(")
"""A perl ``open`` call, the only perl shape that writes a file handle."""

_PERL_MODE = re.compile(r"^>{1,2}$")
"""A pure ``open`` mode literal: ``>`` or ``>>``; a path then follows it."""

_REDIRECT = home_litter_gate._REDIRECT
_FD_REDIRECT = home_litter_gate._FD_REDIRECT
_REMOVE_QUOTES = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")

_MESSAGE = (
    "agent-guards blocked {tool} [plugin_local_edit_gate]: {path} sits inside an INSTALLED plugin "
    "checkout (a runtime copy under ~/.hermes). Installed copies are never edited by hand: "
    "{alternative}"
)
_WITH_CLONE = (
    "the matching working clone is {clone}. Start a worktree there: "
    "`{worktree}`; edit and test in that worktree, commit and push, then sync every installed copy: {sync}."
)
_WITHOUT_CLONE = (
    "no working clone under ~/git_repos matches its origin ({origin}); clone it first: "
    "`git clone {origin} ~/git_repos/{name}`, then start a worktree: "
    "`git -C ~/git_repos/{name} worktree add .worktrees/{slug} -b {branch} origin/main`; "
    "edit and test in that worktree, commit and push, then sync every installed copy: {sync}."
)

_url_cache: dict = {}
"""Resolved origin URLs per work tree, so repeated calls do not re-run git."""


def _home() -> Path:
    return _paths._home()


def _git(path: Path, *args: str) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True, timeout=_GIT_TIMEOUT
        )
    except Exception:
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _nearest_work_tree(path: Path) -> Optional[Path]:
    """Closest enclosing directory holding a .git entry, from *path* upward."""
    for candidate in (path, *path.parents):
        try:
            if (candidate / ".git").exists():
                return candidate
        except OSError:
            continue
    return None


def _origin_url(work_tree: Path) -> Optional[str]:
    key = str(work_tree)
    if key in _url_cache:
        return _url_cache[key]
    url = _git(work_tree, "config", "--get", "remote.origin.url") or None
    if len(_url_cache) > 512:
        _url_cache.clear()
    _url_cache[key] = url
    return url


def _installed_checkout(path: Path) -> Optional[Path]:
    """The plugin checkout root when *path* sits inside an installed git checkout.

    The path must resolve under ``~/.hermes/plugins/`` or
    ``~/.hermes/profiles/<profile>/plugins/`` of the real home, and the nearest
    enclosing git work tree must carry an origin remote. Anything else — a
    locally developed plugin without a checkout, a path outside the home —
    returns ``None`` and stays allowed.
    """
    try:
        parts = path.relative_to(_home()).parts
    except (OSError, ValueError):
        return None
    if not parts or parts[0] != ".hermes":
        return None
    if len(parts) >= 3 and parts[1] == "plugins":
        pass
    elif len(parts) >= 5 and parts[1] == "profiles" and parts[3] == "plugins":
        pass
    else:
        return None
    work_tree = _nearest_work_tree(path)
    if work_tree is None:
        return None
    if not _origin_url(work_tree):
        return None
    return work_tree


def _resolve(token: str, cwd: Optional[str]) -> Optional[Path]:
    try:
        text = token.strip().strip("'\"")
        text = _paths._normalize(text)
        if cwd and not text.startswith(("/", "~", "$")):
            text = _paths._join(cwd, text) or text
        if text.startswith("~"):
            text = str(_home()) + text[1:]
        return Path(text).resolve()
    except Exception:
        return None


def _plugin_name(path: Path) -> Optional[str]:
    """The installed plugin's directory name: the first level under ``plugins``."""
    try:
        parts = path.relative_to(_home()).parts
    except (OSError, ValueError):
        return None
    for index, part in enumerate(parts):
        if part == "plugins":
            if index + 1 < len(parts):
                return parts[index + 1]
            return None
    return None


def _profiles_holding(name: str) -> List[str]:
    """Copies of the <name> plugin: the global copy first, then profile names."""
    found = ["global"]
    root = _home() / ".hermes" / "profiles"
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return found
    for entry in entries:
        try:
            if (entry / "plugins" / name).is_dir():
                found.append(entry.name)
        except OSError:
            continue
    return found


def _sync_commands(name: str) -> str:
    parts = []
    for holder in _profiles_holding(name):
        if holder == "global":
            parts.append(f"hermes plugins update {name}")
        else:
            parts.append(f"hermes -p {holder} plugins update {name}")
    return "; ".join(parts)


def _matching_clone(origin: str) -> Optional[Path]:
    """The working clone under ~/git_repos whose origin URL equals *origin*."""
    root = _home() / "git_repos"
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return None
    for entry in entries:
        try:
            if not (entry / ".git").exists():
                continue
        except OSError:
            continue
        if _origin_url(entry) == origin:
            return entry
    return None


def _message(tool: str, path: Path) -> str:
    work_tree = _nearest_work_tree(path)
    origin = _origin_url(work_tree) if work_tree is not None else None
    name = _plugin_name(path)
    sync = _sync_commands(name or "<plugin>")
    clone = _matching_clone(origin) if origin else None
    if clone is not None:
        worktree = f"git -C {clone} worktree add .worktrees/{name or 'slug'} -b guard-{name or 'slug'} origin/main"
        alternative = _WITH_CLONE.format(clone=clone, worktree=worktree, sync=sync)
    else:
        alternative = _WITHOUT_CLONE.format(
            origin=origin or "<origin-url>", name=name or "<plugin>", slug=name or "slug",
            branch=f"guard-{name or 'slug'}", sync=sync,
        )
    return _MESSAGE.format(tool=tool, path=path, alternative=alternative)


def _git_shape(tokens: List[str]) -> bool:
    """True when a ``git`` segment only inspects the tree (or fast-forwards it).

    ``git checkout -- <file>``, ``git restore`` and ``git apply`` rewrite the
    work tree, so they stay writes; ``fetch``/``status``/``pull --ff-only`` and
    ``rebase origin/...`` are the sanctioned sync shapes and pass.
    """
    try:
        index = tokens.index("git")
    except ValueError:
        return False
    rest = tokens[index + 1:]
    subcommand = None
    position = 0
    while position < len(rest):
        token = rest[position]
        if token == "-C":
            position += 2
            continue
        if token.startswith("-"):
            position += 1
            continue
        subcommand = token
        break
    if subcommand is None:
        return True
    if subcommand in _SAFE_GIT:
        return True
    if subcommand == "pull" and "--ff-only" in rest:
        return True
    if subcommand == "rebase" and any(part.startswith("origin/") for part in rest):
        return True
    return False


def _hermes_shape(segment: str) -> bool:
    """True for a ``hermes`` segment whose verb manages plugins (update, install...)."""
    return re.search(r"\bplugins\s+(update|install|enable|disable|list|doctor)\b", segment) is not None


def _segment_allowed(tokens: List[str], segment: str) -> bool:
    """True when the segment is one of the sanctioned shapes on an installed tree."""
    head = tokens[0].lower() if tokens and not tokens[0].startswith("-") else ""
    if head == "git":
        return _git_shape(tokens)
    if head == "hermes":
        return _hermes_shape(segment)
    return False


def _operand_targets(cwd: Optional[str], segment: str, tokens: List[str]) -> List[str]:
    """Path operands a segment writes into, per head shape; fail-open gaps named in the docstring."""
    targets: List[str] = []
    unquoted = _REMOVE_QUOTES.sub("''", segment)
    for match in _REDIRECT.finditer(unquoted):
        if not _FD_REDIRECT.match(match.group(1)):
            targets.append(_paths._join(cwd or "", match.group(1)))
    index = home_litter_gate._head_index(tokens)
    head = tokens[index].lower() if index < len(tokens) else ""
    operands = [token for token in tokens[index + 1:] if not token.startswith("-")]
    if head in _COPY_HEADS:
        targets.extend(operands[-1:])
    elif head in _TARGET_HEADS:
        targets.extend(operands)
    elif head == "tee":
        targets.append(_paths._join(cwd or "", operands[0]) if operands else "")
    elif head == "sed":
        if _IN_PLACE.search(segment):
            targets.extend(home_litter_gate._sed_destinations(tokens, index))
    elif head == "perl":
        targets.extend(_perl_targets(tokens, index, segment))
    return targets


def _literal_targets(tokens: List[str]) -> List[str]:
    """Paths a python/perl inline payload WRITES, via home_litter's creation-call net.

    Every quoted literal of a ``-c`` payload goes through
    ``home_litter_gate._py_creation_literals``, which returns only the literals
    the creation calls actually write. Feeding raw quoted literals instead
    blocked read paths and prose strings (replay: grep probes, importlib
    harnesses, echo payloads — 14 false blocks in 14 days of history). The
    ``open(path, 'w')`` mode argument follows the path in ``body_literals``;
    unlike that gate's single-literal target shape, an in-place edit's path
    carries a "/" or starts with "~", so non-path payloads (mode chars, prose)
    are dropped here by that shape test.
    """
    payload = " ".join(tokens)
    literals = home_litter_gate._py_creation_literals(payload)
    return [literal for literal in literals if "/" in literal or literal.startswith("~")]


def _perl_targets(tokens: List[str], index: int, segment: str) -> List[str]:
    """Writes a ``perl`` segment performs: in-place file operands and ``open`` paths.

    ``-i``/``-pi`` either as a lone token with an optional suffix argument
    (``-i.bak`` stays one token) or inside a flag cluster, marks the
    non-option operands after the ``-e``/``-p``/``-n`` scripts as in-place
    targets. An ``open`` call contributes the literal after a ``>``/``>>``
    mode, or the ``>path`` part of a shared mode-and-path literal.
    """
    targets: List[str] = []
    rest = tokens[index + 1:]
    in_place = False
    position = 0
    while position < len(rest):
        token = rest[position]
        if token == "-i":
            in_place = True
            if position + 1 < len(rest) and rest[position + 1] == "":
                position += 1
        elif token.startswith("-i") and not token.startswith("--"):
            in_place = True
        elif "i" in token[1:] and token.startswith("-") and not token.startswith("--") and len(token) > 1:
            in_place = any(cluster == "i" for cluster in token[1:])
        elif token in ("-e", "--expression", "-f", "--file"):
            position += 1
        elif token.startswith(("-e", "-f")):
            position += 1
        elif not token.startswith("-") or token == "-":
            if in_place:
                targets.append(token)
        position += 1
    if _PERL_OPEN.search(segment):
        targets.extend(_perl_open_literals(segment))
    return targets


def _perl_open_literals(segment: str) -> List[str]:
    """Path literals a perl ``open`` call would write.

    ``open(F, '>', 'path')`` puts the path after the mode; for the bare
    redirect form ``open(F, '>path')`` the mode and path share one literal,
    and append (``>>``) stays a write shape. Read modes (``<``) never
    contribute.
    """
    literals: List[str] = []
    for match in _PERL_OPEN.finditer(segment):
        body = home_litter_gate._py_call_body(segment, match.start())
        strings = [literal for _, literal in home_litter_gate._PY_STRINGS.findall(body)]
        for position, literal in enumerate(strings):
            if _PERL_MODE.match(literal.lstrip(" ")) and position + 1 < len(strings):
                literals.append(strings[position + 1])
            elif literal.startswith(">"):
                literals.append(literal[1:].strip())
    return literals


def terminal_block(command: str, base_cwd: Optional[str] = None) -> Optional[str]:
    """The block message when a shell command would write an installed plugin checkout."""
    blanked = _paths._strip_heredoc_bodies(str(command or ""))
    for cwd, segment in _paths._segments(blanked, base_cwd):
        tokens = _paths._tokens(segment)
        if _segment_allowed(tokens, segment):
            continue
        head = tokens[0].lower() if tokens else ""
        candidates = _operand_targets(cwd, segment, tokens)
        if head in _LITERAL_HEADS:
            candidates.extend(_literal_targets(tokens))
        if head == "git" and any(token in _MUTATING_GIT_HEADS for token in tokens):
            candidates.extend(
                token for token in tokens[1:]
                if token.startswith("/") or token.startswith("~")
            )
        for candidate in candidates:
            resolved = _resolve(candidate, cwd)
            if resolved is not None and _installed_checkout(resolved) is not None:
                return _message("terminal", resolved)
    return None


def path_block(tool: str, path: str) -> Optional[str]:
    """The block message when a file tool's *path* sits inside an installed checkout."""
    resolved = _resolve(str(path or ""), None)
    if resolved is not None and _installed_checkout(resolved) is not None:
        return _message(tool, resolved)
    return None


def check(tool_name: str, args: Any, **kwargs: Any) -> Optional[str]:
    """``pre_tool_call``: block writes into installed plugin checkouts; fail open."""
    try:
        if not isinstance(args, dict):
            return None
        if tool_name in ("write_file", "patch"):
            return path_block(tool_name, str(args.get("path") or ""))
        if tool_name == "terminal":
            return terminal_block(
                str(args.get("command") or ""), home_litter_gate._resolved_cwd(args, kwargs)
            )
        return None
    except Exception:
        return None


__all__ = ["check", "path_block", "terminal_block"]
