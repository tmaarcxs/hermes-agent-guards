import importlib.util
import sys
from pathlib import Path

_root = Path(__file__).parents[1]
if "agent_guards" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "agent_guards", _root / "__init__.py", submodule_search_locations=[str(_root)]
    )
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["agent_guards"] = _module
    _spec.loader.exec_module(_module)

# Every test run must keep its hands off the operator's real Jev decision log:
# test runs have polluted ~/.hermes/state/jev-decisions.jsonl with fake-card
# entries twice already. Redirect the shared client's log to a scratch file for
# the whole session; tests that exercise the log patch it themselves.
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_jev_log(monkeypatch, tmp_path):
    import agent_guards.jev as _jev

    monkeypatch.setattr(_jev, "_LOG_PATH", str(tmp_path / "jev-decisions.jsonl"))


collect_ignore = [str(_root / "__init__.py")]
