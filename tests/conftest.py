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

collect_ignore = [str(_root / "__init__.py")]
