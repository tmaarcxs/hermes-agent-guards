"""Plugin entry point for ``kanban-card-progress``.

``register(ctx)`` starts ONE daemon thread that sweeps every running Kanban card
every ``interval_seconds`` (~30 s by default), reads the worker's native
Tasklist from the worker profile's ``state.db`` and rewrites the card body's
leading progress block. It is registered ``on_unload`` so a plugin reload or a
gateway shutdown stops the thread cleanly.

Why a thread and not the ``on_kanban_dispatch_tick`` hook: that hook's cadence
is the dispatch interval (10 s here) and it is *shared* with ``agent-guards``'
six tick subscribers and is **not** in ``_HOOK_TIMEOUT_BOUNDED_HOOKS`` — it runs
on the caller thread with no timeout, so a slow sweep would stall every other
subscriber and extend the dispatcher tick. An own daemon thread is independent,
untimed by anything else and trivially bounded.

The plugin is **disabled by default**: it is absent from `plugins.enabled` (the
allow-list that decides whether a user plugin loads at all) — the same
convention `task-retro` follows (`kanban-guards` has no `config_schema`, so it is
not part of it). Enable it with ``hermes plugins enable kanban-card-progress``
(or add the key to ``plugins.enabled``) and restart Hermes. Once loaded, the
``settings.enabled`` switch under ``plugins.entries.kanban-card-progress`` (the
subtree this module reads) turns the sweep off again without unloading the
plugin.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import sweep
from .timer import ProgressTimer

logger = logging.getLogger(__name__)

_LOG_PATH = Path(__file__).resolve().parent / "debug.log"

_PLUGIN_KEY = "kanban-card-progress"
_config_cache: Optional[Dict[str, Any]] = None
_timer: Optional[ProgressTimer] = None


def register(ctx) -> None:
    """Start the sweep thread; register the matching unload hook."""
    ctx.on_unload(_shutdown)
    config = _config()
    if config.get("enabled", True) is False:
        _log("disabled via plugins.entries.kanban-card-progress.enabled — no thread started")
        return
    _start(config, ctx=ctx)


def _start(config: Dict[str, Any], *, ctx: Any = None) -> ProgressTimer:
    global _timer
    if _timer is not None and _timer.running:
        return _timer  # single instance per gateway process
    _timer = ProgressTimer(
        _run_cycle,
        interval_seconds=_float_cfg(config, "interval_seconds", sweep.DEFAULT_INTERVAL_SECONDS),
        log=_log,
        warn=lambda m: logger.warning("%s", m),
    )
    _timer.start()
    _log(
        "started: interval={}s max_cards={} budget={}s".format(
            _timer._interval,
            _int_cfg(config, "max_cards_per_cycle", sweep.DEFAULT_MAX_CARDS),
            _float_cfg(config, "cycle_budget_seconds", sweep.DEFAULT_CYCLE_BUDGET),
        )
    )
    return _timer


def _shutdown() -> None:
    global _timer
    if _timer is not None:
        _timer.stop()
        _log("stopped")
    _timer = None


def _run_cycle() -> None:
    """One sweep, on its own board connection (opened and closed per cycle)."""
    config = _config()
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing() as conn:
        stats = sweep.sweep_once(
            conn,
            max_cards=_int_cfg(config, "max_cards_per_cycle", sweep.DEFAULT_MAX_CARDS),
            max_body_bytes=_int_cfg(config, "max_body_bytes", sweep.DEFAULT_MAX_BODY_BYTES),
            sqlite_timeout=_float_cfg(config, "sqlite_timeout_seconds", sweep.DEFAULT_SQLITE_TIMEOUT),
            budget_seconds=_float_cfg(config, "cycle_budget_seconds", sweep.DEFAULT_CYCLE_BUDGET),
            log=_log,
        )
    _log(f"cycle done: {stats!r}")


# --- config -----------------------------------------------------------------

def _config() -> Dict[str, Any]:
    """This plugin's ``plugins.entries.kanban-card-progress`` config, cached."""
    global _config_cache
    if _config_cache is None:
        try:
            from hermes_cli.config import load_config_readonly

            cfg = load_config_readonly() or {}
            entries = (cfg.get("plugins") or {}).get("entries") or {}
            entry = entries.get(_PLUGIN_KEY) or {}
            settings = entry.get("settings") or entry.get("config") or {}
            _config_cache = settings if isinstance(settings, dict) else {}
        except Exception:
            _config_cache = {}
    return _config_cache


def _reset_config_cache() -> None:
    """Test hook: drop the cached config."""
    global _config_cache
    _config_cache = None


def _int_cfg(config: Dict[str, Any], key: str, default: int) -> int:
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _float_cfg(config: Dict[str, Any], key: str, default: float) -> float:
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _log(msg: str) -> None:
    if not _config().get("debug"):
        return
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass
