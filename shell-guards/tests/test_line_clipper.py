"""Tests for shell-guards long-line clipping on transform_terminal_output."""

import sys

guards = sys.modules["shell_guards"]
clip = guards.line_clipper.clip
transform = guards._transform_terminal_output


def test_clips_runaway_search_and_ps_lines_preserving_head_and_tail():
    """Search and process commands clip non-JSON lines above 560 chars."""
    long_ps = "12345 " + ("A" * 900) + "TAIL_MARKER"
    out = "header\n" + long_ps + "\nfooter\n"
    res = clip(out, command="ps -eo pid,command | grep hermes", returncode=0)
    assert res is not None
    lines = res.splitlines()
    assert lines[0] == "header"
    assert lines[2] == "footer"
    assert lines[1].startswith("12345 " + ("A" * 354))
    assert "...[+" in lines[1] and "chars clipped]..." in lines[1]
    assert lines[1].endswith("TAIL_MARKER")
    assert len(lines[1]) < len(long_ps)


def test_clips_runaway_generic_lines_only_above_wide_threshold():
    """Normal prose paragraphs up to 1040 chars survive untouched; multi-KB blobs clip."""
    prose = "word " * 190
    assert clip(prose, command="cat notes.md", returncode=0) is None

    minified = "PREFIX_" + ("x" * 2500) + "_SUFFIX"
    res = clip("ok\n" + minified + "\ndone", command="cat bundle.js", returncode=0)
    assert res is not None
    mid = res.splitlines()[1]
    assert mid.startswith("PREFIX_")
    assert mid.endswith("_SUFFIX")
    assert "+1594 chars clipped" in mid


def test_skips_nonzero_exit_codes_and_non_strings():
    """Error tracebacks and non-string outputs are never clipped."""
    huge = "E" * 3000
    assert clip(huge, command="pytest -q", returncode=1) is None
    assert clip(huge, command="grep foo bar", returncode=2) is None
    assert clip({"ok": True}, command="grep foo bar", returncode=0) is None


def test_skips_pure_json_documents_and_complete_jsonl_lines():
    """Structured JSON documents and per-line JSON objects/arrays stay intact."""
    doc = '{"items": ["' + ("z" * 3000) + '"]}'
    assert clip(doc, command="gh pr view 1 --json body", returncode=0) is None

    mixed = "=== PR ===\n" + doc + "\n=== END ===\n"
    assert clip(mixed, command="echo header; gh pr view 1 --json body", returncode=0) is None

    arr_line = '[{"k": "' + ("v" * 2000) + '"}]'
    assert clip("ok\n" + arr_line + "\n", command="grep foo bar", returncode=0) is None


def test_skips_explicit_char_or_byte_slice_commands():
    """Explicit cut -c / head -c / tail -c slices are honored verbatim."""
    line = "X" * 3000
    assert clip(line, command="cut -c1-3000 transcript.txt", returncode=0) is None
    assert clip(line, command="head -c 3000 blob.txt", returncode=0) is None
    assert clip(line, command="tail -c 3000 blob.txt", returncode=0) is None


def test_heredoc_mentioning_grep_does_not_trigger_tight_threshold():
    """Only the command header outside a heredoc selects the tight search threshold."""
    line = "P" * 750
    cmd = "python3 - <<'EOF'\nprint('grep test')\nEOF"
    assert clip(line, command=cmd, returncode=0) is None


def test_hook_entrypoint_clips_rc0_and_keeps_rc124_timeout_note():
    """The registered transform_terminal_output hook dispatches rc=0 to line_clipper."""
    long_line = "H" * 1500
    out = transform(command="ps aux", output=long_line, returncode=0)
    assert isinstance(out, str) and "chars clipped" in out
    assert transform(command="ps aux", output="short", returncode=0) is None
