"""Tests for the task-retro skill rules and the session_dump WAL fix.

Run:  python3 -m pytest tests/test_skill_rules.py -q

Covers:
- the consolidate-first / one-home / live-verification write gates are present in the
  skill's own SKILL.md and references, and every file stays under its size gate;
- ``session_dump.connect`` survives a live WAL database whose ``mode=ro`` open fails
  with sqlite error 14 (synthetic reproduction), without writing to the source.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
SKILL_DIR = PLUGIN_DIR / "skills" / "task-retrospective"
SKILL_MD = SKILL_DIR / "SKILL.md"
REFERENCE_LIMIT = 12000
SKILL_LIMIT = 6000


def _load_session_dump():
    spec = importlib.util.spec_from_file_location(
        "session_dump_under_test", SKILL_DIR / "scripts" / "session_dump.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- session_dump WAL fix ---------------------------------------------------


def _make_wal_db(path: Path, rows: int = 3) -> sqlite3.Connection:
    """Create a WAL database and keep the connection open (data stays in -wal)."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    return conn


def _copy_main_only(dest_dir: Path, src: Path) -> Path:
    """Copy only the main file into a fresh subdir, no sidecars (the absent-sidecar case).

    The main file keeps its WAL journal-mode header; without -wal/-shm a plain
    ``mode=ro`` open of it fails with error 14.
    """
    target = dest_dir / f"copy-{src.parent.name}"
    target.mkdir()
    dest = target / src.name
    shutil.copyfile(src, dest)
    return dest


def _naive_read(path: Path):
    """The pre-fix path: open mode=ro and execute."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    return conn.execute("SELECT count(*) FROM t").fetchone()


@pytest.fixture
def wal_db(tmp_path):
    src = tmp_path / "live.db"
    keep = _make_wal_db(src)
    yield src
    keep.close()


@pytest.fixture
def closed_wal_db(tmp_path):
    """A WAL-header database whose data is checkpointed into the main file.

    Closing the writer flushes the table into the main file while the header
    stays in WAL journal mode; copying only the main file then yields the
    "sidecars absent" state a live host can present.
    """
    src = tmp_path / "closed.db"
    _make_wal_db(src).close()
    return src


def test_mode_ro_reproduces_error_14_without_sidecars(wal_db, tmp_path):
    """Reproduction: a WAL-header db with no -wal/-shm fails under mode=ro."""
    copy = _copy_main_only(tmp_path, wal_db)
    assert sorted(p.name for p in copy.parent.iterdir()) == [wal_db.name]
    with pytest.raises(sqlite3.OperationalError):
        _naive_read(copy)


def test_immutable_ro_reads_the_absent_sidecar_db(closed_wal_db, tmp_path):
    """The mechanism the fallback relies on: immutable=1 reads the absent-sidecar copy."""
    copy = _copy_main_only(tmp_path, closed_wal_db)
    conn = sqlite3.connect(f"file:{copy}?mode=ro&immutable=1", uri=True)
    assert conn.execute("SELECT count(*) FROM t").fetchone() == (3,)
    conn.close()


def test_connect_falls_back_and_reads_the_snapshot(closed_wal_db, tmp_path):
    """The fix: connect() survives the absent-sidecar case its docstring names."""
    mod = _load_session_dump()
    copy = _copy_main_only(tmp_path, closed_wal_db)
    conn = mod.connect(str(copy))
    assert conn.execute("SELECT count(*) FROM t").fetchone() == (3,)
    conn.close()


def test_connect_does_not_write_to_the_source(closed_wal_db, tmp_path):
    """The fallback must never touch the source db or create a sidecar beside it.

    The source sits in a read-only directory, so any attempt to create -wal/-shm
    beside it would fail; connect() must still return a usable connection.
    """
    mod = _load_session_dump()
    copy = _copy_main_only(tmp_path, closed_wal_db)
    before = sorted(p.name for p in copy.parent.iterdir())
    size_before = copy.stat().st_size
    os.chmod(copy.parent, 0o500)
    try:
        mod.connect(str(copy)).close()
    finally:
        os.chmod(copy.parent, 0o700)
    assert sorted(p.name for p in copy.parent.iterdir()) == before
    assert copy.stat().st_size == size_before


def test_connect_tolerates_an_unreadable_sidecar(wal_db):
    """An unreadable -wal is skipped, not raised: connect() still returns a connection."""
    mod = _load_session_dump()
    wal = wal_db.with_name(wal_db.name + "-wal")
    assert wal.exists(), "fixture must leave a -wal behind"
    os.chmod(wal, 0o000)
    try:
        conn = mod.connect(str(wal_db))
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        conn.close()
    finally:
        os.chmod(wal, 0o600)


def test_connect_happy_path_uses_mode_ro_in_place(wal_db):
    """A normal live db is read in place (no snapshot dir created)."""
    mod = _load_session_dump()
    mod._CLEANUP.clear()
    conn = mod.connect(str(wal_db))
    assert conn.execute("SELECT count(*) FROM t").fetchone() == (3,)
    conn.close()
    assert mod._CLEANUP == []


def test_snapshot_is_removed_at_exit(tmp_path):
    """The temp snapshot dir is registered for atexit cleanup."""
    mod = _load_session_dump()
    src = tmp_path / "s.db"
    keep = _make_wal_db(src)
    try:
        snap = mod._snapshot(src)
        assert snap.is_file()
        assert snap.parent in mod._CLEANUP
        mod._cleanup()
        assert not snap.parent.exists()
    finally:
        keep.close()


# --- skill rules and size gates --------------------------------------------


def _skill_text() -> str:
    parts = [SKILL_MD.read_text(encoding="utf-8")]
    for ref in sorted((SKILL_DIR / "references").glob("*.md")):
        parts.append(ref.read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_frontmatter_valid_and_name_unchanged():
    text = SKILL_MD.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    assert match, "SKILL.md must start with YAML frontmatter"
    front = match.group(1)
    assert re.search(r"^name:\s*task-retrospective\s*$", front, re.M)
    assert re.search(r"^description:\s*\S", front, re.M)
    assert re.search(r"^version:\s*\S", front, re.M)


def test_size_gates_pass():
    assert SKILL_MD.stat().st_size <= SKILL_LIMIT, "SKILL.md over its 6000-char gate"
    for ref in sorted((SKILL_DIR / "references").glob("*.md")):
        size = ref.stat().st_size
        assert size <= REFERENCE_LIMIT, f"{ref.name} over its 12000-char gate ({size})"


@pytest.mark.parametrize("needle", [
    "one home",            # rule 1
    "Consolidate",         # rule 2
    "topic reference",     # rule 3
    "re-grep",             # rule 4
    "deferred",            # rule 5
    "gate",                # rule 6
])
def test_rules_present_in_skill(needle):
    assert needle in _skill_text(), f"rule keyword missing from the skill: {needle}"


def test_one_home_and_live_verification_gates_in_actions():
    actions = (SKILL_DIR / "references" / "actions.md").read_text(encoding="utf-8")
    for gate in ("One finding, one home", "Consolidate first",
                 "Verify live", "Never patch the plugin's own skill copy"):
        assert gate in actions, f"write gate missing from actions.md: {gate}"
    # the pre-existing gates the plugin's own test asserts must survive
    for gate in ("Already-covered check first", "Smallest justified correction", "Read back"):
        assert gate in actions


def test_plugin_copy_is_never_the_write_target():
    text = _skill_text()
    assert "overwritten by" in text  # the plugins-install warning is stated
