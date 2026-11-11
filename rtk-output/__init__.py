"""Filter completed foreground terminal output, never the command or its exit code.

RTK pipe is deliberately used instead of command rewriting: interpreter paths,
env unsets, shell state and scripts keep their native execution semantics. Failed
or ambiguous output stays raw. A shortened result is returned only after its full,
redacted input has been durably saved. All failures leave Hermes' output unchanged.
"""

import json
import logging
import re
import shutil
import subprocess
import time
import uuid

_BAD = re.compile(r"\b(?:failed|errors?|traceback)\b|no tests (?:ran|collected)|OUTPUT TRUNCATED", re.I)
_SUMMARY = re.compile(r"^(?:=+\s*)?((?:\d+ (?:passed|skipped|deselected|xfailed|xpassed|warnings?),?\s*)+)in [\d.]+s(?:\s*=+)?\Z", re.M)
_COUNTS = re.compile(r"\b(\d+) (passed|skipped|deselected|xfailed|xpassed|warnings?)\b")
_LIMIT = 2 * 1024 * 1024
_QUOTA = 64 * 1024 * 1024
_LOG = logging.getLogger(__name__)


def transform(command="", output="", returncode=None, env_type="", **kwargs):
    try:
        if not env_type or returncode != 0 or not isinstance(output, str) or not 2048 <= len(output) <= _LIMIT:
            return None
        from agent.redact import redact_terminal_output
        from tools.ansi_strip import strip_ansi
        from tools.spill_safety import ensure_spill_dir, write_text_exclusive
        from hermes_constants import get_hermes_home

        clean = redact_terminal_output(strip_ansi(output), command, force=True)
        if _BAD.search(clean) or len(clean.encode()) > _LIMIT:
            return None
        binary = shutil.which("rtk")
        if not binary:
            return None
        summary = _SUMMARY.search(clean.rstrip())
        argv = [binary, "pipe"] + (["--filter", "pytest"] if summary else [])
        result = subprocess.run(argv, input=clean, capture_output=True, text=True, timeout=2)
        if result.returncode or result.stderr.strip() or not result.stdout.strip():
            return None
        shown = redact_terminal_output(strip_ansi(result.stdout), command, force=True)
        if _BAD.search(shown):
            return None
        if summary and not all(re.search(rf"\b{n} {status}\b", shown) for n, status in _COUNTS.findall(summary[1])):
            return None
        root = ensure_spill_dir(get_hermes_home() / "plugin-data" / "rtk-output" / "raw")
        now = time.time()
        for path in root.iterdir():
            if path.suffix in (".txt", ".json") and path.lstat().st_mtime < now - 7 * 86400:
                path.unlink()
        if sum(path.lstat().st_size for path in root.iterdir()) >= _QUOTA:
            return None
        key = uuid.uuid4().hex
        raw = root / f"{key}.txt"
        receipt = root / f"{key}.json"
        replacement = shown.rstrip() + f"\n[RTK: full redacted output: {raw}]"
        before, after = len(clean.encode()), len(replacement.encode())
        if after >= before:
            return None
        write_text_exclusive(raw, clean)
        try:
            write_text_exclusive(receipt, json.dumps({"filter": "pytest" if summary else "auto", "input_bytes": before, "shown_bytes": after, "exit_code": returncode}))
        except Exception:
            raw.unlink(missing_ok=True)
            raise
        return replacement
    except Exception:
        _LOG.debug("RTK output filter unavailable; preserving terminal output", exc_info=True)
        return None


def register(ctx):
    ctx.register_hook("transform_terminal_output", transform)
